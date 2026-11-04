"""Command line: rra verify | signoff | status | audit-check | graph"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .audit import verify_chain
from .pipeline import (DECISIONS, VALID_RESOLUTIONS, build_graph, mermaid, resume_run,
                       start_run)
from .support import OLLAMA_HOST, OllamaUnavailable


def cmd_verify(a: argparse.Namespace) -> int:
    mode = "heuristic" if a.heuristic else "ollama"
    try:
        result, run_dir = start_run(Path(a.manuscript), Path(a.out), Path(a.cache), mode=mode,
                                    model=a.model, offline=a.offline, mailto=a.mailto,
                                    ollama_host=a.ollama_host, run_id=a.run_id)
    except OllamaUnavailable as e:
        print(f"Error: {e}", file=sys.stderr)
        return 3
    ev = [e["verdict"] for e in result["existence"]]
    sv = [s["verdict"] for s in result["support"]]
    ok, msg = verify_chain(run_dir / "audit.jsonl")
    print(f"Run {result['run_id']}  (checker: {mode}{':' + result['model'] if result.get('model') else ''})")
    print(f"  references: {len(ev)}  verified={ev.count('verified')}  "
          f"not_found={ev.count('not_found')}  retracted={ev.count('retracted')}  "
          f"mismatch={ev.count('metadata_mismatch')}  found_by_search={ev.count('found_by_search')}  "
          f"unchecked={ev.count('unchecked')}")
    print(f"  claim-source pairs: {len(sv)}  supported={sv.count('supported')}  "
          f"partial={sv.count('partially_supported')}  not_supported={sv.count('not_supported')}  "
          f"needs_review={sv.count('needs_human_review')}  insufficient={sv.count('insufficient_evidence')}")
    print(f"  report: {run_dir / 'report.html'}")
    print(f"  audit log: {msg}")
    print(f"  PAUSED for human review. Resume with:\n    rra signoff {run_dir} --reviewer NAME "
          f"--decision {{{'|'.join(DECISIONS)}}}")
    return 0


def cmd_signoff(a: argparse.Namespace) -> int:
    resolutions = []
    for res in a.resolve or []:
        item, _, rest = res.partition("=")
        verdict, _, note = rest.partition(":")
        if verdict not in VALID_RESOLUTIONS:
            print(f"Unknown verdict '{verdict}'. Use one of: {', '.join(sorted(VALID_RESOLUTIONS))}",
                  file=sys.stderr)
            return 2
        resolutions.append({"item": item.strip(), "verdict": verdict, "note": note.strip()})
    review = {"reviewer": a.reviewer, "decision": a.decision, "note": a.note or "",
              "resolutions": resolutions}
    try:
        result = resume_run(Path(a.run_dir), review, cache_dir=Path(a.cache))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    print(f"Signed off ({result['status']}). Report: {Path(a.run_dir) / 'report.html'}")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    run_dir = Path(a.run_dir)
    snap = build_graph(run_dir.parent / "checkpoints.sqlite").get_state(
        {"configurable": {"thread_id": run_dir.name}})
    if not snap.values:
        print(f"No checkpointed run named {run_dir.name}")
        return 1
    print(f"{run_dir.name}: {snap.values.get('status')}  next={list(snap.next) or 'done'}")
    for t in snap.tasks:
        for i in t.interrupts:
            v = i.value
            print(f"  references needing attention: {v['references_needing_attention']}")
            print(f"  claim pairs needing attention: {v['claim_pairs_needing_attention']}")
    return 0


def cmd_audit(a: argparse.Namespace) -> int:
    ok, msg = verify_chain(Path(a.log))
    print(("OK: " if ok else "TAMPERED: ") + msg)
    return 0 if ok else 1


def cmd_graph(a: argparse.Namespace) -> int:
    print(mermaid(Path(a.out)))
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import os
    import uvicorn
    os.environ["RRA_RUNS"], os.environ["RRA_CACHE"] = a.out, a.cache
    os.environ["RRA_OLLAMA_HOST"] = a.ollama_host
    uvicorn.run("rra.api:default_app", factory=True, host=a.host, port=a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rra", description="Responsible Research Agent — citation & claim verifier")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="Verify a manuscript; pauses for human review")
    v.add_argument("manuscript", help="Markdown or plain-text file with a 'References' section")
    v.add_argument("--out", default="runs", help="Output folder for runs (default: runs)")
    v.add_argument("--cache", default=".rra_cache", help="Response cache folder (default: .rra_cache)")
    v.add_argument("--offline", action="store_true", help="Replay from cache only; no network or model calls")
    v.add_argument("--heuristic", action="store_true", help="No model; support checks go to human review")
    v.add_argument("--model", help="Ollama model (default: $RRA_MODEL or llama3.1:8b)")
    v.add_argument("--ollama-host", default=OLLAMA_HOST, help=f"Ollama URL (default: {OLLAMA_HOST})")
    v.add_argument("--mailto", help="Contact email for Crossref/OpenAlex polite pool (optional)")
    v.add_argument("--run-id", help=argparse.SUPPRESS)
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("signoff", help="Resume a paused run with the human decision")
    s.add_argument("run_dir")
    s.add_argument("--reviewer", required=True)
    s.add_argument("--decision", required=True, choices=DECISIONS)
    s.add_argument("--note")
    s.add_argument("--cache", default=".rra_cache")
    s.add_argument("--resolve", action="append", metavar="ITEM=VERDICT[:note]",
                   help="Resolve one item, e.g. C3/2=supported:'checked full text p.4' or R5=not_found")
    s.set_defaults(func=cmd_signoff)

    st = sub.add_parser("status", help="Show where a run is in the graph")
    st.add_argument("run_dir")
    st.set_defaults(func=cmd_status)

    c = sub.add_parser("audit-check", help="Check an audit log has not been altered")
    c.add_argument("log")
    c.set_defaults(func=cmd_audit)

    gr = sub.add_parser("graph", help="Print the workflow as a Mermaid diagram")
    gr.add_argument("--out", default="runs")
    gr.set_defaults(func=cmd_graph)

    sv = sub.add_parser("serve", help="Start the API server for the web app")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--out", default="runs")
    sv.add_argument("--cache", default=".rra_cache")
    sv.add_argument("--ollama-host", default=OLLAMA_HOST)
    sv.set_defaults(func=cmd_serve)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
