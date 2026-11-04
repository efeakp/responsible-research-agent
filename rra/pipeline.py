"""The verification workflow as a LangGraph graph.

    parse ─► verify_references ─► check_support ─► prepare_report ─► human_review ─► finalise
                                                                       (interrupt)

- State is checkpointed to SQLite after every node (thread_id = run_id), so a
  run can stop at human_review, survive a restart, and be resumed days later
  by `rra signoff`.
- human_review calls LangGraph's interrupt(): the graph genuinely pauses until
  a person supplies a decision. There is no path to "finalised" without one.
- Non-serialisable things (HTTP transport, file handles) live in the runtime
  context, not in state, so they are never written to the checkpoint.
"""

from __future__ import annotations

import platform
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TypedDict

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from . import __version__
from .audit import AuditLog, sha256_text, utcnow, verify_chain
from .existence import verify_reference
from .http import CachedClient
from .models import (Claim, ExistenceResult, ExistenceVerdict, Reference, RunReport,
                     SupportResult, SupportVerdict)
from .parse import extract_claims, parse_references, split_manuscript
from .report import render
from .support import (DEFAULT_MODEL, OLLAMA_HOST, SYSTEM_PROMPT, HeuristicChecker, LLMChecker,
                      ollama_model_info)

DECISIONS = ("approve", "approve_with_changes", "reject")
VALID_RESOLUTIONS = {v.value for v in ExistenceVerdict} | {v.value for v in SupportVerdict}


class State(TypedDict, total=False):
    run_id: str
    run_dir: str
    manuscript: str
    manuscript_sha256: str
    started_at: str
    mode: str                     # "ollama" | "heuristic"
    model: Optional[str]
    offline: bool
    references: list[dict]
    claims: list[dict]
    existence: list[dict]
    support: list[dict]
    unresolved_citations: list[str]
    uncited_references: list[str]
    review: Optional[dict]
    status: str


@dataclass
class Ctx:
    cache_dir: str
    mailto: Optional[str] = None
    ollama_host: str = OLLAMA_HOST
    transport: Any = None         # httpx transport override (tests)


# ---------- helpers ----------

def _audit(state: State) -> AuditLog:
    return AuditLog(Path(state["run_dir"]) / "audit.jsonl")


def _http(state: State, ctx: Ctx, audit: AuditLog) -> CachedClient:
    return CachedClient(Path(ctx.cache_dir) / "http", audit, offline=state.get("offline", False),
                        mailto=ctx.mailto, transport=ctx.transport)


def _report(state: State) -> RunReport:
    return RunReport(
        run_id=state["run_id"], manuscript=state["manuscript"],
        manuscript_sha256=state["manuscript_sha256"], started_at=state["started_at"],
        finished_at=utcnow(), tool_version=__version__,
        references=[ExistenceResult.model_validate(e) for e in state.get("existence", [])],
        claims=[Claim.model_validate(c) for c in state.get("claims", [])],
        support=[SupportResult.model_validate(s) for s in state.get("support", [])],
        unresolved_citations=state.get("unresolved_citations", []),
        uncited_references=state.get("uncited_references", []))


# ---------- nodes ----------

def parse_node(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    text = Path(state["manuscript"]).read_text(encoding="utf-8")
    audit = _audit(state)
    model_info = None
    if state["mode"] == "ollama" and not state.get("offline"):
        http = _http(state, ctx, audit)
        model_info = ollama_model_info(http, state["model"], ctx.ollama_host)  # raises if unavailable
    audit.log("run_started", run_id=state["run_id"], manuscript=state["manuscript"],
              manuscript_sha256=sha256_text(text), tool_version=__version__,
              python=platform.python_version(), offline=state.get("offline", False),
              mode=state["mode"], model=model_info or state.get("model"),
              support_prompt_sha256=sha256_text(SYSTEM_PROMPT) if state["mode"] == "ollama" else None)
    body, ref_section = split_manuscript(text)
    refs = parse_references(ref_section)
    claims = extract_claims(body)
    audit.log("parsed", references=[r.model_dump(mode="json", exclude_none=True) for r in refs],
              claims=[c.model_dump(mode="json") for c in claims])
    if not refs:
        audit.log("warning", message="No reference list found (expected a 'References' heading "
                                     "followed by numbered entries)")
    ref_keys = {r.key for r in refs}
    cited = {k for c in claims for k in c.cited_keys}
    return {"manuscript_sha256": sha256_text(text),
            "references": [r.model_dump(mode="json") for r in refs],
            "claims": [c.model_dump(mode="json") for c in claims],
            "unresolved_citations": sorted(cited - ref_keys, key=int),
            "uncited_references": sorted(ref_keys - cited, key=int)}


def verify_references_node(state: State, runtime: Runtime[Ctx]) -> dict:
    audit = _audit(state)
    http = _http(state, runtime.context, audit)
    results = [verify_reference(http, audit, Reference.model_validate(r))
               for r in state["references"]]
    return {"existence": [r.model_dump(mode="json") for r in results]}


def check_support_node(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    audit = _audit(state)
    http = _http(state, ctx, audit)
    if state["mode"] == "ollama":
        checker = LLMChecker(http, audit, Path(ctx.cache_dir) / "llm", model=state["model"],
                             host=ctx.ollama_host)
    else:
        checker = HeuristicChecker()
    records = {e["reference"]["key"]: ExistenceResult.model_validate(e).record
               for e in state["existence"]}
    out: list[dict] = []
    for c in state["claims"]:
        claim = Claim.model_validate(c)
        for key in claim.cited_keys:
            if key not in records:
                continue
            rec = records[key]
            res = checker.check(claim, key, rec)
            if rec is not None and rec.is_retracted:
                res.rationale = ("Source is RETRACTED; it should not be relied on whatever its "
                                 "text says. " + res.rationale).strip()
            audit.log("support_verdict", claim_id=claim.id, reference_key=key,
                      verdict=res.verdict.value, method=res.method,
                      evidence_quote=res.evidence_quote, rationale=res.rationale)
            out.append(res.model_dump(mode="json"))
    return {"support": out}


def prepare_report_node(state: State) -> dict:
    run_dir = Path(state["run_dir"])
    report_json = _report(state).model_dump_json(indent=2)
    (run_dir / "report.json").write_text(report_json, encoding="utf-8")
    _audit(state).log("report_prepared", report_sha256=sha256_text(report_json),
                      status="awaiting_human_signoff")
    render(run_dir)
    return {"status": "awaiting_human_signoff"}


def human_review_node(state: State) -> dict:
    # Everything before interrupt() re-runs on resume, so nothing here has side effects.
    attention = [s for s in state["support"] if s["verdict"] != "supported"]
    bad_refs = [e for e in state["existence"] if e["verdict"] != "verified"]
    decision = interrupt({
        "run_id": state["run_id"],
        "report": str(Path(state["run_dir"]) / "report.html"),
        "references_needing_attention": [e["reference"]["key"] for e in bad_refs],
        "claim_pairs_needing_attention": [f"{s['claim_id']}/{s['reference_key']}" for s in attention],
        "instructions": "Resume with {'reviewer', 'decision', 'note', 'resolutions': [{item, verdict, note}]}",
    })
    if not isinstance(decision, dict) or decision.get("decision") not in DECISIONS \
            or not decision.get("reviewer"):
        raise ValueError(f"Review must include a reviewer and a decision in {DECISIONS}")
    audit = _audit(state)
    for r in decision.get("resolutions", []):
        if r.get("verdict") not in VALID_RESOLUTIONS:
            raise ValueError(f"Unknown verdict {r.get('verdict')!r}")
        audit.log("human_resolution", item=r["item"], verdict=r["verdict"],
                  note=r.get("note", ""), reviewer=decision["reviewer"])
    audit.log("human_signoff", reviewer=decision["reviewer"], decision=decision["decision"],
              note=decision.get("note", ""))
    return {"review": decision}


def finalise_node(state: State) -> dict:
    run_dir = Path(state["run_dir"])
    _audit(state).log("run_finished", status="signed_off",
                      decision=state["review"]["decision"],
                      report_sha256=sha256_text((run_dir / "report.json").read_text(encoding="utf-8")))
    render(run_dir)
    return {"status": f"signed_off:{state['review']['decision']}"}


# ---------- graph ----------

_GRAPHS: dict[str, Any] = {}
_GRAPHS_LOCK = threading.Lock()


def build_graph(checkpoint_path: Path):
    """One compiled graph (and one SQLite connection) per checkpoint file per process,
    so the CLI, tests and the API server never open competing writers."""
    key = str(Path(checkpoint_path).resolve())
    with _GRAPHS_LOCK:
        if key not in _GRAPHS:
            _GRAPHS[key] = _compile(Path(checkpoint_path))
        return _GRAPHS[key]


def _compile(checkpoint_path: Path):
    g = StateGraph(State, context_schema=Ctx)
    g.add_node("parse", parse_node)
    g.add_node("verify_references", verify_references_node)
    g.add_node("check_support", check_support_node)
    g.add_node("prepare_report", prepare_report_node)
    g.add_node("human_review", human_review_node)
    g.add_node("finalise", finalise_node)
    g.add_edge(START, "parse")
    g.add_edge("parse", "verify_references")
    g.add_edge("verify_references", "check_support")
    g.add_edge("check_support", "prepare_report")
    g.add_edge("prepare_report", "human_review")
    g.add_edge("human_review", "finalise")
    g.add_edge("finalise", END)
    Path(checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn))


def new_run_id() -> str:
    return f"{utcnow().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:6]}"


def run_snapshot(out_dir: Path, run_id: str):
    return build_graph(Path(out_dir) / "checkpoints.sqlite").get_state(
        {"configurable": {"thread_id": run_id}})


def start_run(manuscript: Path, out_dir: Path, cache_dir: Path, *, mode: str = "ollama",
              model: Optional[str] = None, offline: bool = False, mailto: Optional[str] = None,
              ollama_host: str = OLLAMA_HOST, transport: httpx.BaseTransport | None = None,
              run_id: Optional[str] = None) -> tuple[dict, Path]:
    """Run until the human-review interrupt. Returns (graph result, run_dir)."""
    run_id = run_id or new_run_id()
    run_dir = Path(out_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    graph = build_graph(Path(out_dir) / "checkpoints.sqlite")
    if graph.get_state({"configurable": {"thread_id": run_id}}).values:
        raise RuntimeError(f"A run named {run_id} already exists; resume it with `rra signoff`")
    init: State = {"run_id": run_id, "run_dir": str(run_dir), "manuscript": str(manuscript),
                   "started_at": utcnow(), "mode": mode,
                   "model": (model or DEFAULT_MODEL) if mode == "ollama" else None,
                   "offline": offline, "status": "running"}
    ctx = Ctx(cache_dir=str(cache_dir), mailto=mailto, ollama_host=ollama_host, transport=transport)
    result = graph.invoke(init, {"configurable": {"thread_id": run_id}}, context=ctx)
    return result, run_dir


def resume_run(run_dir: Path, review: dict, cache_dir: Path = Path(".rra_cache"),
               transport: httpx.BaseTransport | None = None) -> dict:
    """Supply the human decision and let the graph finish."""
    run_dir = Path(run_dir)
    ok, msg = verify_chain(run_dir / "audit.jsonl")
    if not ok:
        raise RuntimeError(f"Refusing to resume: audit log is broken ({msg})")
    graph = build_graph(run_dir.parent / "checkpoints.sqlite")
    config = {"configurable": {"thread_id": run_dir.name}}
    snapshot = graph.get_state(config)
    if "human_review" not in (snapshot.next or ()):
        raise RuntimeError(f"Run {run_dir.name} is not waiting for review "
                           f"(status: {snapshot.values.get('status', 'unknown')})")
    return graph.invoke(Command(resume=review), config, context=Ctx(cache_dir=str(cache_dir),
                                                                     transport=transport))


def mermaid(out_dir: Path = Path("runs")) -> str:
    return build_graph(Path(out_dir) / "checkpoints.sqlite").get_graph().draw_mermaid()
