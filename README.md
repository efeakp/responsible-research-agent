# Responsible Research Agent — v0.3

A citation and claim verifier. It runs as a **LangGraph** workflow and judges claims with a **local LLM via Ollama**. No manuscript text leaves your machine except for the bibliographic lookups (DOIs, titles).

```
parse ─► verify_references ─► check_support ─► prepare_report ─► human_review ─► finalise
                                                                   (interrupt)
```

1. **parse** splits the manuscript into body and reference list. It extracts DOIs, arXiv IDs and PMIDs, and finds every sentence that carries a citation.
2. **verify_references** resolves each reference against Crossref, OpenAlex, arXiv and PubMed. It compares title, first author and year, and flags retractions. References without an identifier are matched by title search across OpenAlex, Crossref and, for preprints, the arXiv API (`search_query=ti:"…"`). A match is accepted only when confidence is high.
3. **check_support** asks the local model whether each cited source's abstract supports the claim. The model must quote its evidence, and the quote is checked word-for-word against the abstract. If it isn't there, the verdict is withheld and goes to human review. Any numbers in the claim that are missing from the source are flagged.
4. **prepare_report** writes `report.html` and `report.json`.
5. **human_review** pauses the graph with LangGraph's `interrupt()`. State is checkpointed to SQLite, so the run can wait days. It finishes only when a person signs off.
6. **finalise** records the decision and re-renders the report.

Every database request, model call and verdict goes into a hash-chained audit log (`audit.jsonl`). The Ollama model's **digest** is recorded so you know exactly which weights produced each verdict. Model calls use temperature 0 and a fixed seed, and all responses are cached, so `--offline` replays a run exactly.

## Install

```bash
# 1. Ollama: https://ollama.com/download, then
ollama pull llama3.1:8b

# 2. This package (needs Python 3.10 or newer: check with `python --version`)
cd responsible-research-agent        # the folder containing pyproject.toml
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash
# .venv\Scripts\activate            # Windows Command Prompt / PowerShell
# source .venv/bin/activate          # macOS / Linux
python -m pip install --upgrade pip  # editable installs need pip 21.3 or newer
pip install -e ".[test]"
```

With conda instead of venv: `conda create -n rra python=3.12 -y`, then `conda activate rra`, then the same pip steps.

It has three parts:

| Part | Where | What it does |
|---|---|---|
| Engine | `rra/` | LangGraph workflow, database lookups, Ollama checks, audit log |
| Backend | `rra/api.py` (FastAPI) | Accepts uploads, runs checks on a background worker, serves results, takes sign-off |
| Frontend | `frontend/` (React + Vite + TypeScript) | Upload, progress, review and sign-off in the browser |

## Web app

You need two terminals.

```bash
# terminal 1: the API (http://127.0.0.1:8000, docs at /docs)
rra serve

# terminal 2: the React app (http://localhost:5173)
cd frontend
npm install
npm run dev
```

You can upload a PDF, Word (.docx) or HTML article as well as Markdown. These need the converter installed on the API side: run `pip install -e ".[convert]"`. The app converts the file and shows you the text that will be checked, with counts of references, identifiers and citations, plus any warnings. You can correct the text there before starting. The original file is kept with the run. The first entry in the audit trail records the original's fingerprint, the converter used, and whether you edited the text.

In development, Vite forwards `/api` to port 8000. To deploy the frontend separately, build it with `VITE_API_URL=https://your-api-host npm run build`. Then add that frontend's origin to `cors_origins` in `create_app`.

**Try the web app with no network and no Ollama:** run `python examples/serve_demo.py` instead of `rra serve`, then upload `examples/sample_manuscript.md`. The demo uses mocked databases and a scripted stand-in for the model.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Whether Ollama is reachable, and which models are pulled |
| POST | `/api/conversions` | Convert a PDF, Word or HTML file (`file`) for preview. Returns the Markdown, counts and warnings |
| POST | `/api/inspect` | `{markdown}`. What the verifier will see: counts and missing citations |
| GET | `/api/runs` | All checks, newest first |
| POST | `/api/runs` | Queue a run from a `.md`/`.txt` `file`, or from a `conversion_id` plus the (edited) `markdown`. Also takes `mode` and `model`. Returns 202 |
| GET | `/api/runs/{id}` | Results, pending items, reviewer verdicts, sign-off |
| POST | `/api/runs/{id}/review` | `{reviewer, decision, note, resolutions[]}`. Resumes the graph from its review pause |
| GET | `/api/runs/{id}/audit` | Audit events and whether the hash chain is intact |
| GET | `/api/runs/{id}/report` | The static HTML report |
| GET | `/api/runs/{id}/source` | The original PDF, Word or HTML file, if the run came from a conversion |
| GET | `/api/runs/{id}/manuscript` | The exact Markdown that was checked |

Runs are processed one at a time, which respects arXiv's one-connection rule. The server binds to `127.0.0.1` by default. **It has no login.** Don't expose it on a network until authentication is added.

## Try the engine without network or Ollama

```bash
python examples/demo_offline.py --signoff   # mocked APIs and a scripted stand-in model
pytest -q                                   # 51 tests (engine, API, converter)
```

## Converting articles to Markdown

The verifier reads Markdown. To convert a PDF, Word (.docx) or HTML article:

```bash
pip install -e ".[convert]"          # once
rra convert paper.pdf                # writes paper.md
rra convert paper.docx -o paper.md
```

It re-joins hyphenated words and wrapped lines, removes page numbers, and turns superscript citation numbers into `[n]`. It also finds the reference list and splits it into one numbered entry per line. If the original used Word's automatic numbering, which doesn't survive extraction, it numbers the entries itself. Sections after the references, such as acknowledgements or an appendix, are moved before them.

It then reports what the verifier will see: how many references were found, how many have a DOI, arXiv ID or PMID, how many cited sentences there are, and any citations with no matching reference. Anything it had to guess is printed as a warning. **Skim the output before verifying it.** PDF extraction is never perfect, and superscript citations in PDFs are the most common thing to go missing.

For better PDF layout handling, also run `pip install pymupdf4llm`; it's used automatically when installed. It is AGPL-licensed, so it's left out of the default install.

## Command line

```bash
rra verify examples/sample_manuscript.md --mailto you@example.org
rra status runs/<run-id>        # where the run is, and what needs attention
rra signoff runs/<run-id> --reviewer "Efe A" --decision approve_with_changes \
    --resolve "C2/1=not_supported:overstates the source" --note "Fix C2 before submission"
```

- `--model` or `RRA_MODEL` picks the Ollama model (default `llama3.1:8b`). `--ollama-host` or `OLLAMA_HOST` sets the server (default `http://localhost:11434`).
- If Ollama isn't running or the model isn't pulled, `verify` stops with a clear error. It does not quietly fall back. Use `--heuristic` for a run with no model; claim support then always goes to human review.
- Item names: `C<claim>/<ref>` for claim support, `R<ref>` for references.
- Sign-off is refused if the audit log has been altered, or if the run is not waiting for review.
- `rra graph` prints the workflow as a Mermaid diagram.
- arXiv requests are spaced at least 3 seconds apart, one at a time, as the [arXiv API terms of use](https://info.arxiv.org/help/api/tou.html) require. Cached responses are not throttled, so re-runs are fast.

Each run writes `runs/<run-id>/` containing `report.html`, `report.json` and `audit.jsonl`. Checkpoints for all runs are stored in `runs/checkpoints.sqlite`.

## Sources outside the scholarly databases

Many legitimate sources aren't in Crossref, OpenAlex, arXiv or PubMed: government and regulator reports, think-tank papers, standards, legislation, web pages, and datasets with DataCite DOIs (Zenodo, Figshare). The checker tries not to treat these as missing:

1. **DOIs not known to Crossref** are checked at doi.org, which knows every registered DOI whatever the agency. If doi.org has metadata (DataCite usually does), the title, author and year are compared as normal and the reference can be *verified*. If the DOI is registered but has no metadata, it is marked *exists, not indexed*. Only a DOI that isn't registered anywhere is *not found*.
2. **Links** are opened to check they work. The checker reads only the page title, not the whole document, and ignores site suffixes like "- GOV.UK" when matching titles. A working link is *exists, not indexed*. A broken one (404 or 410) is *not found*, with a link to search the Internet Archive. If a site refuses automated checks (403, rate limits), the result is *couldn't check*, never *not found*. Links to this computer or the local network are never opened.
3. **No DOI and no link:** if the reference is formatted like a journal or conference paper (a volume and pages, or a journal name) and no database has it, it stays red *not found*, because that's the usual pattern of a fabricated reference. Otherwise it's *can't verify*: a person has to check it.

These sources have no abstract to compare against, so their claim checks come back as *no text to check*, with a prompt to check the claim against the source yourself.

**Documents without numbered citations**, such as author–year styles, can still be checked: the reference list is verified, and the run says no claims were checked.

## Choosing a model

Small local models are weaker judges than large hosted ones. The design compensates:

- The output is constrained to a JSON schema, using Ollama structured outputs.
- A *supported* verdict needs a verbatim quote that passes a mechanical check.
- Number mismatches are caught deterministically.
- A person signs off everything.

Expect more *needs human review* results from an 8B model than from a larger one. If your hardware allows, compare a few models on the same manuscript. Each run logs the model digest, so the comparison is auditable.

## Verdicts

| Stage | Verdict | Meaning |
|---|---|---|
| Reference | verified | Identifier resolves; title, first author and year match |
| | metadata_mismatch | Resolves, but the record is a different work or details disagree |
| | found_by_search | No identifier given; confident title match found |
| | exists_unindexed | Real, but not a scholarly record: a DOI registered with another agency that has no metadata, or a working link. Check by hand that it is the cited document |
| | unverifiable | No DOI or link, and not in the databases. Typical of reports, standards and legislation. Check by hand |
| | not_found | The DOI, arXiv ID, PMID or link doesn't exist, or the reference is formatted like a journal paper that no database has |
| | retracted | Exists, but retracted |
| | unchecked | Lookup failed (network etc.). Never treated as "not found" |
| Claim | supported | Model verdict backed by a verbatim quote from the source |
| | partially_supported | Overstated, generalised, or the numbers differ |
| | not_supported | The source does not say this |
| | insufficient_evidence | No abstract available |
| | needs_human_review | Heuristic mode, or the model's evidence failed the quote check |

## Known limitations

The plan for addressing these is in [ROADMAP.md](ROADMAP.md).


- Claim support uses abstracts only, not full text.
- Only numeric citation styles are supported.
- Reference-line parsing is heuristic. Read the issue text before acting on a mismatch.
- The audit log is tamper-evident, not tamper-proof.
- Test fixtures use real metadata but **synthetic abstract text**. The first live run is the real test.

## Layout

```
rra/pipeline.py   LangGraph graph: nodes, checkpointer, start/resume
rra/parse.py      manuscript → references + cited claims
rra/sources.py    Crossref, OpenAlex, arXiv, PubMed resolvers
rra/existence.py  existence, metadata match, retraction
rra/support.py    claim support (Ollama + quote check, or heuristic)
rra/audit.py      hash-chained log
rra/http.py       cached, logged HTTP
rra/report.py     HTML report
rra/api.py        FastAPI backend
rra/convert.py    PDF / Word / HTML → verifier-ready Markdown
rra/cli.py        verify | status | signoff | audit-check | graph | serve | convert
frontend/src/     React app: RunsPage (upload + list), RunPage (review sheet, sign-off, audit trail)
```
