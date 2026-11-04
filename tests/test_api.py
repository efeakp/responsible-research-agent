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
