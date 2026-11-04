import json
from pathlib import Path

import httpx
import pytest

from rra.audit import verify_chain
from rra.cli import main as cli_main
from rra.parse import _clean_doi, extract_claims
from rra.pipeline import build_graph, resume_run, start_run
from rra.support import OllamaUnavailable, number_issues

from fixtures import make_transport, scripted_llm

SAMPLE = Path(__file__).parent.parent / "examples" / "sample_manuscript.md"
HOST = "http://localhost:11434"


def go(tmp, run_id="t1", **kw):
    kw.setdefault("transport", make_transport(scripted_llm))
    kw.setdefault("mode", "ollama")
    return start_run(SAMPLE, tmp / "runs", tmp / "cache", ollama_host=HOST, run_id=run_id, **kw)


@pytest.fixture
def paused(tmp_path):
    result, run_dir = go(tmp_path)
    return result, run_dir, tmp_path


def verdicts(result):
    return {e["reference"]["key"]: e["verdict"] for e in result["existence"]}


def support(result):
    return {(s["claim_id"], s["reference_key"]): s for s in result["support"]}


def test_existence_verdicts(paused):
    assert verdicts(paused[0]) == {"1": "verified", "2": "verified", "3": "not_found",
                                   "4": "retracted", "5": "metadata_mismatch", "6": "found_by_search"}


def test_citation_integrity(paused):
    assert paused[0]["unresolved_citations"] == ["7"]
    assert paused[0]["uncited_references"] == []


def test_supported_claim_has_verbatim_quote(paused):
    s = support(paused[0])[("C1", "1")]
    assert s["verdict"] == "supported" and s["evidence_quote"]
    assert s["method"] == "ollama:llama3.1:8b"


def test_fabricated_quote_is_never_supported(paused):
    s = support(paused[0])[("C2", "1")]
    assert s["verdict"] == "needs_human_review"
    assert "not in the source" in s["rationale"] and "90" in s["rationale"]


def test_retracted_source_flagged_in_support(paused):
    s = support(paused[0])[("C5", "4")]
    assert s["verdict"] == "insufficient_evidence" and "RETRACTED" in s["rationale"]


def test_model_digest_logged(paused):
    events = [json.loads(l) for l in (paused[1] / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    started = next(e for e in events if e["event"] == "run_started")
    assert started["data"]["model"]["digest"] == "sha256-test"


def test_graph_pauses_at_human_review(paused):
    result, run_dir, _ = paused
    assert result["status"] == "awaiting_human_signoff"
    assert "__interrupt__" in result
    snap = build_graph(run_dir.parent / "checkpoints.sqlite").get_state(
        {"configurable": {"thread_id": run_dir.name}})
    assert snap.next == ("human_review",)
    assert "Awaiting human sign-off" in (run_dir / "report.html").read_text(encoding="utf-8")


def test_resume_via_cli_finishes_run(paused):
    _, run_dir, tmp = paused
    rc = cli_main(["signoff", str(run_dir), "--reviewer", "Tester", "--cache", str(tmp / "cache"),
                   "--decision", "approve_with_changes", "--note", "fix C2",
                   "--resolve", "C2/1=not_supported:overstates the source"])
    assert rc == 0
    snap = build_graph(run_dir.parent / "checkpoints.sqlite").get_state(
        {"configurable": {"thread_id": run_dir.name}})
    assert snap.next == () and snap.values["status"] == "signed_off:approve_with_changes"
    assert verify_chain(run_dir / "audit.jsonl")[0]
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "approve with changes" in html and "overstates the source" in html
    # a finished run cannot be signed off twice
    with pytest.raises(RuntimeError, match="not waiting"):
        resume_run(run_dir, {"reviewer": "X", "decision": "approve"}, cache_dir=tmp / "cache")


def test_tampered_log_blocks_signoff(paused):
    _, run_dir, tmp = paused
    log = run_dir / "audit.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[3]); ev["data"]["tampered"] = True
    lines[3] = json.dumps(ev, sort_keys=True, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ok, msg = verify_chain(log)
    assert not ok and "line 4" in msg
    with pytest.raises(RuntimeError, match="audit log is broken"):
        resume_run(run_dir, {"reviewer": "X", "decision": "approve"}, cache_dir=tmp / "cache")


def test_offline_replay_reproduces_verdicts(paused):
    result, _, tmp = paused
    def no_network(request):
        raise AssertionError(f"network call during offline replay: {request.url}")
    replay, _ = go(tmp, run_id="t2", offline=True, transport=httpx.MockTransport(no_network))
    assert verdicts(replay) == verdicts(result)
    assert {k: v["verdict"] for k, v in support(replay).items()} == \
           {k: v["verdict"] for k, v in support(result).items()}


def test_ollama_down_fails_loudly(tmp_path):
    with pytest.raises(OllamaUnavailable, match="not reachable"):
        go(tmp_path, transport=make_transport(scripted_llm, fail_hosts={"localhost:11434"}))


def test_missing_model_fails_loudly(tmp_path):
    with pytest.raises(OllamaUnavailable, match="ollama pull"):
        go(tmp_path, model="qwen3:8b")


def test_network_failure_is_unchecked_not_missing(tmp_path):
    result, _ = go(tmp_path, mode="heuristic",
                   transport=make_transport(fail_hosts={"api.crossref.org", "api.openalex.org"}))
    v = verdicts(result)
    assert v["1"] == "unchecked" and v["3"] == "unchecked" and v["2"] == "verified"


def test_heuristic_mode_never_says_supported(tmp_path):
    result, _ = go(tmp_path, mode="heuristic", transport=make_transport())
    assert all(s["verdict"] != "supported" for s in result["support"])


def test_parser_edge_cases():
    assert _clean_doi("10.1016/S0140-6736(97)11096-0).") == "10.1016/s0140-6736(97)11096-0"
    claims = extract_claims("Smith et al. found this [1–3]. Fig. 2 shows nothing. Also [4,5].")
    assert claims[0].cited_keys == ["1", "2", "3"]
    assert claims[1].cited_keys == ["4", "5"]
    assert number_issues("Rates fell by 40% [1].", "rates fell by about 25%")


def test_arxiv_title_search_finds_preprint_without_identifier(tmp_path):
    from rra.audit import AuditLog
    from rra.existence import verify_reference
    from rra.http import CachedClient
    from rra.models import Reference
    audit = AuditLog(tmp_path / "a.jsonl")
    http = CachedClient(tmp_path / "c", audit, transport=make_transport())
    ref = Reference(key="9", raw="Vaswani A, et al. (2017). Attention is all you need.",
                    title="Attention is all you need", first_author="Vaswani", year=2017)
    res = verify_reference(http, audit, ref)
    assert res.verdict.value == "found_by_search"
    assert res.record.source == "arxiv" and res.record.identifier == "1706.03762"


def test_arxiv_requests_are_spaced_three_seconds(tmp_path, monkeypatch):
    import rra.http as h
    from rra.audit import AuditLog
    from rra.sources import arxiv_by_id
    now = [1000.0]; waits = []
    monkeypatch.setattr(h, "MIN_INTERVAL", {"export.arxiv.org": 3.0})
    monkeypatch.setattr(h, "_clock", lambda: now[0])
    monkeypatch.setattr(h, "_sleep", lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
    http = h.CachedClient(tmp_path / "c", AuditLog(tmp_path / "a.jsonl"), transport=make_transport())
    arxiv_by_id(http, "1706.03762")          # first call: no wait
    now[0] += 1.0
    arxiv_by_id(http, "9999.99999")          # 1 s later: must wait 2 s
    arxiv_by_id(http, "1706.03762")          # cache hit: no wait
    assert waits == [pytest.approx(2.0)]
