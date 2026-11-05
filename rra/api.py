"""FastAPI backend.

    GET  /api/health                 Ollama reachability and pulled models
    GET  /api/runs                   all runs, newest first
    POST /api/conversions            convert a PDF / Word / HTML article for preview
    POST /api/inspect                what the verifier will see in some Markdown
    POST /api/runs                   queue a run from a .md/.txt file or a conversion (202)
    GET  /api/runs/{id}              full results, review state, pending items
    POST /api/runs/{id}/review       record the human decision; resumes the graph
    GET  /api/runs/{id}/audit        audit events and chain integrity
    GET  /api/runs/{id}/report       the static HTML report
    GET  /api/runs/{id}/source       the original PDF / Word / HTML file, if converted
    GET  /api/runs/{id}/manuscript   the exact Markdown that was checked

Runs execute one at a time on a background worker: a live run takes minutes
(local model, 3-second arXiv spacing), and arXiv asks for one connection at a time.
The review endpoint resumes the LangGraph run from its human_review interrupt.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__
from .audit import AuditLog, sha256_text, utcnow, verify_chain
from .convert import ConversionUnavailable, convert_file, inspect_markdown
from .pipeline import DECISIONS, VALID_RESOLUTIONS, new_run_id, resume_run, run_snapshot, start_run
from .support import DEFAULT_MODEL, OLLAMA_HOST

RUN_ID_RE = re.compile(r"^[0-9A-Za-z\-]{6,64}$")
MAX_UPLOAD = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}
CONVERTIBLE_SUFFIXES = {".pdf", ".docx", ".html", ".htm", ".xhtml"}
MAX_CONVERT_UPLOAD = 25 * 1024 * 1024


class Resolution(BaseModel):
    item: str = Field(description="R<ref> or C<claim>/<ref>")
    verdict: str
    note: str = ""


class InspectIn(BaseModel):
    markdown: str = Field(max_length=MAX_UPLOAD)


class ReviewIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=120)
    decision: Literal["approve", "approve_with_changes", "reject"]
    note: str = ""
    resolutions: list[Resolution] = []


def create_app(runs_dir: Path = Path("runs"), cache_dir: Path = Path(".rra_cache"), *,
               ollama_host: str = OLLAMA_HOST, mailto: Optional[str] = None,
               transport: httpx.BaseTransport | None = None,
               cors_origins: Optional[list[str]] = None) -> FastAPI:
    runs_dir, cache_dir = Path(runs_dir), Path(cache_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    mailto = mailto or os.environ.get("RRA_MAILTO")
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rra-run")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        worker.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="Responsible Research Agent", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware,
                       allow_origins=cors_origins or ["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.jobs = jobs
    app.state.worker = worker

    # ---------- helpers ----------

    def run_dir_for(run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(404, "Run not found")
        d = runs_dir / run_id
        if not (d / "meta.json").exists():
            raise HTTPException(404, "Run not found")
        return d

    def read_json(p: Path) -> Optional[dict]:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def status_of(run_id: str) -> tuple[str, Optional[str], dict]:
        """(status, error, checkpoint values). Live job state wins over the checkpoint."""
        with jobs_lock:
            job = dict(jobs.get(run_id, {}))
        values = run_snapshot(runs_dir, run_id).values or {}
        if job.get("status") in ("queued", "running", "failed"):
            return job["status"], job.get("error"), values
        err = read_json(runs_dir / run_id / "error.json")
        if err:
            return "failed", err.get("error"), values
        if values.get("status"):
            return values["status"], None, values
        return "interrupted", "The server stopped before this run finished. Upload it again.", values

    def human_events(run_dir: Path) -> tuple[list[dict], list[dict]]:
        resolutions, signoffs = [], []
        log = run_dir / "audit.jsonl"
        if not log.exists():
            return resolutions, signoffs
        for line in log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev["event"] == "human_resolution":
                resolutions.append({**ev["data"], "ts": ev["ts"]})
            elif ev["event"] == "human_signoff":
                signoffs.append({**ev["data"], "ts": ev["ts"]})
        return resolutions, signoffs

    def counts(values: dict) -> dict:
        ev = [e["verdict"] for e in values.get("existence", [])]
        sv = [s["verdict"] for s in values.get("support", [])]
        return {"references": len(ev), "verified": ev.count("verified"),
                "reference_problems": len(ev) - ev.count("verified"),
                "claims": len(values.get("claims", [])), "pairs": len(sv),
                "supported": sv.count("supported"),
                "pairs_needing_attention": len(sv) - sv.count("supported")}

    def execute(run_id: str, manuscript: Path, mode: str, model: Optional[str]) -> None:
        with jobs_lock:
            jobs[run_id]["status"] = "running"
        try:
            start_run(manuscript, runs_dir, cache_dir, mode=mode, model=model, mailto=mailto,
                      ollama_host=ollama_host, transport=transport, run_id=run_id)
            with jobs_lock:
                jobs.pop(run_id, None)
        except Exception as e:  # surface every failure to the UI; nothing fails silently
            msg = f"{type(e).__name__}: {e}"
            (runs_dir / run_id / "error.json").write_text(json.dumps({"error": msg, "ts": utcnow()}),
                                                         encoding="utf-8")
            with jobs_lock:
                jobs[run_id] = {"status": "failed", "error": msg}

    # ---------- routes ----------

    @app.get("/api/health")
    def health() -> dict:
        info: dict[str, Any] = {"version": __version__, "default_model": DEFAULT_MODEL,
                                "ollama": {"host": ollama_host, "reachable": False, "models": []}}
        try:
            with httpx.Client(transport=transport, timeout=3) as c:
                r = c.get(f"{ollama_host}/api/tags")
                r.raise_for_status()
                info["ollama"]["reachable"] = True
                info["ollama"]["models"] = sorted(m.get("name", "") for m in r.json().get("models", []))
        except Exception as e:
            info["ollama"]["error"] = type(e).__name__
        return info

    @app.get("/api/runs")
    def list_runs() -> list[dict]:
        out = []
        for d in sorted(runs_dir.iterdir(), reverse=True):
            meta = read_json(d / "meta.json") if d.is_dir() else None
            if not meta:
                continue
            status, error, values = status_of(d.name)
            out.append({**meta, "status": status, "error": error, "counts": counts(values)})
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return out

    conv_root = runs_dir / "_conversions"

    def conversion_dir(conversion_id: str) -> Path:
        if not RUN_ID_RE.match(conversion_id or ""):
            raise HTTPException(404, "Conversion not found. Upload the file again.")
        d = conv_root / conversion_id
        if not (d / "conversion.json").exists():
            raise HTTPException(404, "Conversion not found. Upload the file again.")
        return d

    @app.post("/api/conversions", status_code=201)
    def create_conversion(file: UploadFile = File(...)) -> dict:
        """Convert a PDF, Word or HTML article to Markdown for preview. Nothing is checked yet."""
        name = Path(file.filename or "article").name
        suffix = Path(name).suffix.lower()
        if suffix not in CONVERTIBLE_SUFFIXES:
            raise HTTPException(415, "Upload a PDF, Word (.docx) or HTML file.")
        data = file.file.read(MAX_CONVERT_UPLOAD + 1)
        if len(data) > MAX_CONVERT_UPLOAD:
            raise HTTPException(413, "File is larger than 25 MB.")
        conversion_id = new_run_id()
        d = conv_root / conversion_id
        d.mkdir(parents=True)
        original = d / f"original{suffix}"
        original.write_bytes(data)
        try:
            conv = convert_file(original)
        except ConversionUnavailable as e:  # missing optional library, unsupported type
            raise HTTPException(422, str(e))
        except Exception as e:             # corrupt or unreadable file
            raise HTTPException(422, f"Couldn't read this file ({type(e).__name__}). "
                                     "If it's a scanned PDF, it has no text layer to extract.")
        (d / "converted.md").write_text(conv.markdown, encoding="utf-8")
        record = {"conversion_id": conversion_id, "filename": name, "created_at": utcnow(),
                  "extractor": conv.extractor, "warnings": conv.warnings,
                  "original_sha256": sha256_text(data), "converted_sha256": sha256_text(conv.markdown),
                  "numbered_by_converter": conv.numbered_by_converter}
        (d / "conversion.json").write_text(json.dumps(record), encoding="utf-8")
        return {**record, "markdown": conv.markdown, "summary": inspect_markdown(conv.markdown)}

    @app.post("/api/inspect")
    def inspect_text(body: InspectIn) -> dict:
        """What the verifier will see in this Markdown. Used to refresh counts while editing."""
        return inspect_markdown(body.markdown)

    @app.post("/api/runs", status_code=202)
    async def create_run(file: Optional[UploadFile] = File(None),
                         conversion_id: Optional[str] = Form(None),
                         markdown: Optional[str] = Form(None),
                         mode: Literal["ollama", "heuristic"] = Form("ollama"),
                         model: Optional[str] = Form(None)) -> dict:
        """Start a check from an uploaded .md/.txt file, or from a previewed conversion
        (conversion_id plus the Markdown as the reviewer left it)."""
        source = None
        if conversion_id:
            cd = conversion_dir(conversion_id)
            source = read_json(cd / "conversion.json")
            text = markdown if markdown is not None else (cd / "converted.md").read_text(encoding="utf-8")
            # browsers send form text with CRLF line endings; normalise so an unedited preview
            # isn't recorded as "edited"
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if len(text.encode("utf-8")) > MAX_UPLOAD:
                raise HTTPException(413, "Converted text is larger than 2 MB.")
            name = source["filename"]
        elif file is not None:
            name = Path(file.filename or "manuscript.md").name
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                raise HTTPException(415, "Upload a Markdown or plain-text file (.md or .txt), or convert "
                                         "a PDF, Word or HTML file first.")
            data = await file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "File is larger than 2 MB.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise HTTPException(415, "File is not UTF-8 text.")
        else:
            raise HTTPException(422, "Send a file, or a conversion_id from /api/conversions.")
        if not text.strip():
            raise HTTPException(422, "The manuscript is empty.")

        run_id = new_run_id()
        d = runs_dir / run_id
        d.mkdir(parents=True)
        manuscript = d / "manuscript.md"
        manuscript.write_text(text, encoding="utf-8")
        meta = {"run_id": run_id, "filename": name, "created_at": utcnow(), "mode": mode,
                "model": (model or DEFAULT_MODEL) if mode == "ollama" else None}
        if source:
            original = next(cd.glob("original.*"))
            shutil.copy2(original, d / original.name)
            edited = sha256_text(text) != source["converted_sha256"]
            meta["source"] = {"filename": source["filename"], "extractor": source["extractor"],
                              "warnings": source["warnings"], "edited": edited,
                              "original_file": original.name}
            # first entry in the run's audit chain: what the checked text was derived from
            AuditLog(d / "audit.jsonl").log(
                "source_converted", filename=source["filename"], extractor=source["extractor"],
                original_sha256=source["original_sha256"], converted_sha256=source["converted_sha256"],
                checked_sha256=sha256_text(text), edited_by_reviewer=edited,
                converter_warnings=source["warnings"])
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        with jobs_lock:
            jobs[run_id] = {"status": "queued"}
        worker.submit(execute, run_id, manuscript, mode, meta["model"])
        return {"run_id": run_id, "status": "queued"}

    @app.get("/api/runs/{run_id}/source")
    def source_file(run_id: str) -> FileResponse:
        d = run_dir_for(run_id)
        src = (read_json(d / "meta.json") or {}).get("source")
        if not src:
            raise HTTPException(404, "This check was started from a Markdown file, not a conversion.")
        return FileResponse(d / src["original_file"], filename=src["filename"])

    @app.get("/api/runs/{run_id}/manuscript")
    def manuscript_text(run_id: str) -> FileResponse:
        return FileResponse(run_dir_for(run_id) / "manuscript.md", media_type="text/markdown; charset=utf-8")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        d = run_dir_for(run_id)
        status, error, values = status_of(run_id)
        resolutions, signoffs = human_events(d)
        pending = None
        snap = run_snapshot(runs_dir, run_id)
        for t in snap.tasks:
            for i in t.interrupts:
                pending = i.value
        return {**read_json(d / "meta.json"), "status": status, "error": error,
                "counts": counts(values),
                "existence": values.get("existence", []), "claims": values.get("claims", []),
                "support": values.get("support", []),
                "unresolved_citations": values.get("unresolved_citations", []),
                "uncited_references": values.get("uncited_references", []),
                "pending": pending, "resolutions": resolutions, "signoffs": signoffs,
                "verdict_options": sorted(VALID_RESOLUTIONS), "decisions": list(DECISIONS)}

    @app.post("/api/runs/{run_id}/review")
    def review(run_id: str, body: ReviewIn) -> dict:
        d = run_dir_for(run_id)
        status, _, values = status_of(run_id)
        if status != "awaiting_human_signoff":
            raise HTTPException(409, f"This run is not waiting for review (status: {status}).")
        refs = {f"R{e['reference']['key']}" for e in values.get("existence", [])}
        pairs = {f"{s['claim_id']}/{s['reference_key']}" for s in values.get("support", [])}
        for r in body.resolutions:
            if r.item not in refs | pairs:
                raise HTTPException(422, f"Unknown item {r.item!r}.")
            if r.verdict not in VALID_RESOLUTIONS:
                raise HTTPException(422, f"Unknown verdict {r.verdict!r}.")
        try:
            resume_run(d, body.model_dump(), cache_dir=cache_dir, transport=transport)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return get_run(run_id)

    @app.get("/api/runs/{run_id}/audit")
    def audit(run_id: str) -> dict:
        d = run_dir_for(run_id)
        log = d / "audit.jsonl"
        if not log.exists():
            return {"ok": None, "message": "No audit log yet.", "events": []}
        ok, msg = verify_chain(log)
        events = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        return {"ok": ok, "message": msg, "events": events}

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str) -> FileResponse:
        p = run_dir_for(run_id) / "report.html"
        if not p.exists():
            raise HTTPException(404, "The report is written when the run reaches review.")
        return FileResponse(p, media_type="text/html")

    return app


def default_app() -> FastAPI:
    """Factory for `uvicorn --factory rra.api:default_app` (used by `rra serve`)."""
    return create_app(Path(os.environ.get("RRA_RUNS", "runs")),
                      Path(os.environ.get("RRA_CACHE", ".rra_cache")),
                      ollama_host=os.environ.get("RRA_OLLAMA_HOST", OLLAMA_HOST))
