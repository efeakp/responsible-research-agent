import time

import pytest
from fastapi.testclient import TestClient

from rra.api import create_app

from fixtures import make_transport, scripted_llm

SAMPLE = (__import__("pathlib").Path(__file__).parent.parent / "examples" / "sample_manuscript.md").read_bytes()


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "runs", tmp_path / "cache", ollama_host="http://localhost:11434",
                     transport=make_transport(scripted_llm))
    with TestClient(app) as c:
        yield c


def wait(client, run_id, until=("awaiting_human_signoff", "failed"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] in until:
            return r
        time.sleep(0.1)
    raise AssertionError(f"run stuck at {r['status']}")


def upload(client, data=SAMPLE, name="paper.md", **form):
    return client.post("/api/runs", files={"file": (name, data, "text/markdown")},
                       data={"mode": "ollama", **form})


def test_health_lists_models(client):
    h = client.get("/api/health").json()
    assert h["ollama"]["reachable"] and h["ollama"]["models"] == ["llama3.1:8b"]


def test_full_flow_upload_review_signoff(client):
    r = upload(client)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "awaiting_human_signoff"
    assert run["counts"]["references"] == 6 and run["pending"]["claim_pairs_needing_attention"]
    assert client.get(f"/api/runs/{run_id}/report").status_code == 200
    listed = client.get("/api/runs").json()
    assert listed[0]["run_id"] == run_id and listed[0]["filename"] == "paper.md"

    bad = client.post(f"/api/runs/{run_id}/review", json={
        "reviewer": "T", "decision": "approve",
        "resolutions": [{"item": "C99/1", "verdict": "supported"}]})
    assert bad.status_code == 422

    done = client.post(f"/api/runs/{run_id}/review", json={
        "reviewer": "Tester", "decision": "approve_with_changes", "note": "fix C2",
        "resolutions": [{"item": "C2/1", "verdict": "not_supported", "note": "overstated"}]})
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "signed_off:approve_with_changes"
    assert body["resolutions"][0]["item"] == "C2/1" and body["signoffs"][0]["reviewer"] == "Tester"
    assert client.post(f"/api/runs/{run_id}/review",
                       json={"reviewer": "X", "decision": "approve"}).status_code == 409
    audit = client.get(f"/api/runs/{run_id}/audit").json()
    assert audit["ok"] and audit["events"][-1]["event"] == "run_finished"


def test_failed_run_is_reported(client):
    run_id = upload(client, model="qwen3:8b").json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "failed" and "ollama pull" in run["error"]


def test_rejects_bad_uploads(client):
    assert upload(client, name="paper.pdf").status_code == 415
    assert upload(client, data=b"\xff\xfe\x00bad").status_code == 415


def test_unknown_run_404(client):
    assert client.get("/api/runs/nope-123456").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404


# ---------- conversion flow ----------

def test_convert_preview_edit_and_check(client, tmp_path):
    pytest.importorskip("docx")
    from test_convert import make_docx
    src = tmp_path / "paper.docx"
    make_docx(src)
    r = client.post("/api/conversions", files={"file": ("paper.docx", src.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 201, r.text
    conv = r.json()
    assert conv["summary"]["references"] == 3 and conv["summary"]["claims"] >= 3
    assert any("numbered 3 entries" in w for w in conv["warnings"])

    # reviewer edits the text before checking; counts refresh through /api/inspect
    edited = conv["markdown"].replace("updated in 2021[3].", "updated in 2021[3]. Unsourced claim [9].")
    info = client.post("/api/inspect", json={"markdown": edited}).json()
    assert info["missing_citations"] == ["9"]

    run_id = client.post("/api/runs", data={"conversion_id": conv["conversion_id"],
                                            "markdown": edited, "mode": "ollama"}).json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "awaiting_human_signoff"
    assert run["filename"] == "paper.docx" and run["source"]["edited"] is True
    assert run["unresolved_citations"] == ["9"]

    audit = client.get(f"/api/runs/{run_id}/audit").json()
    assert audit["ok"]
    first = audit["events"][0]
    assert first["event"] == "source_converted" and first["data"]["edited_by_reviewer"] is True
    assert first["data"]["original_sha256"] == conv["original_sha256"]

    assert client.get(f"/api/runs/{run_id}/source").content == src.read_bytes()
    assert "Unsourced claim [9]" in client.get(f"/api/runs/{run_id}/manuscript").text


def test_conversion_rejects_unsupported_and_corrupt(client):
    assert client.post("/api/conversions", files={"file": ("a.exe", b"MZ", "application/octet-stream")}).status_code == 415
    r = client.post("/api/conversions", files={"file": ("broken.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 422 and "Couldn't read" in r.json()["detail"]
    assert client.post("/api/runs", data={"conversion_id": "nope-000000", "mode": "heuristic"}).status_code == 404
    assert client.post("/api/runs", data={"mode": "heuristic"}).status_code == 422


def test_author_year_check_with_grey_literature(client):
    md = ("# Heat networks briefing\n\nHeat networks could supply a fifth of UK heat by 2050 (CCC, 2023). "
          "Government set out its plan in 2023 (DESNZ, 2023).\n\n## References\n\n"
          "1. Climate Change Committee (2023). Progress in reducing emissions: 2023 Report to Parliament. "
          "London: CCC.\n"
          "2. DESNZ (2023). Powering up Britain. https://www.gov.uk/government/publications/powering-up-britain\n")
    info = client.post("/api/inspect", json={"markdown": md}).json()
    assert info["citation_style"] == "author_year" and info["claims"] == 2
    assert info["missing_citations"] == ["CCC, 2023: no matching reference"]
    run_id = upload(client, data=md.encode(), name="briefing.md").json()["run_id"]
    run = wait(client, run_id)
    assert run["status"] == "awaiting_human_signoff"
    assert [c["cited_keys"] for c in run["claims"]] == [[], ["2"]]
    assert run["unresolved_citations"] == ["CCC, 2023: no matching reference"]
    assert [e["verdict"] for e in run["existence"]] == ["unverifiable", "exists_unindexed"]


def test_reference_only_check_when_text_cites_nothing(client):
    md = ("# Briefing\n\nHeat networks are growing.\n\n## References\n\n"
          "1. Climate Change Committee (2023). Progress in reducing emissions: 2023 Report to Parliament. "
          "London: CCC.\n")
    run_id = upload(client, data=md.encode(), name="plain.md").json()["run_id"]
    run = wait(client, run_id)
    assert run["claims"] == [] and run["counts"]["pairs"] == 0
    report = client.get(f"/api/runs/{run_id}/report").text
    assert "only the reference list was checked" in report


def test_unedited_preview_with_crlf_is_not_marked_edited(client):
    pytest.importorskip("docx")
    from pathlib import Path
    import tempfile
    from test_convert import make_docx
    src = Path(tempfile.mkdtemp()) / "p.docx"
    make_docx(src)
    conv = client.post("/api/conversions", files={"file": ("p.docx", src.read_bytes(), "application/octet-stream")}).json()
    crlf = conv["markdown"].replace("\n", "\r\n")            # what a browser form sends
    run_id = client.post("/api/runs", data={"conversion_id": conv["conversion_id"], "markdown": crlf,
                                            "mode": "heuristic"}).json()["run_id"]
    assert wait(client, run_id)["source"]["edited"] is False
