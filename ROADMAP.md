# Roadmap

Each milestone below is also a GitHub milestone, with one issue per item. Run `scripts/create_milestones.py` to create them. The list is generated from `.github/milestones.json`, so edit that file, not this one.

No dates are set. Add a `due_on` (for example `2027-01-31T23:59:59Z`) to any milestone in the JSON to give it one.

## v0.4 — Proven on live data

Everything so far is tested against mocked APIs. Run it for real, fix what breaks, and put CI in place so it stays working.

- **First live run against Crossref, OpenAlex, arXiv, PubMed and Ollama** (engine)
- **Test on three real manuscripts in different citation formats** (engine)
- **GitHub Actions: pytest and frontend build on every push** (infra)
- **Check install and run on Windows** (infra)
- **Record live API responses as test fixtures (metadata only)** (engine)

## v0.5 — Full-text evidence

Judging claims against abstracts alone misses most of what papers say. Retrieve open-access full text and check claims against the right passage.

- **Fetch open-access full text (OpenAlex OA locations, Unpaywall, Europe PMC, arXiv PDF)** (engine)
- **Passage retrieval: find the most relevant chunks for each claim** (engine)
- **Show evidence scope (abstract vs full text) in report and UI** (frontend, engine)
- **New LangGraph node: retrieve_evidence between verify_references and check_support** (engine)

## v0.6 — Real-world manuscripts

Accept the formats researchers actually use.

- **Author–year citations, e.g. (Smith et al., 2020)** (engine)
- **Upload .docx and PDF manuscripts** (engine, backend, frontend)
- **Accept BibTeX/RIS reference files** (engine)

## v0.7 — Measured performance

Replace the pitch's targets (zero hallucinated citations, 50–70% time saved) with numbers measured on labelled data and a pilot.

- **Benchmark claim-support verdicts on a labelled claim–evidence dataset** (evaluation)
- **Fabricated-citation detection test set** (evaluation)
- **Compare local models on the benchmark** (evaluation)
- **Time-on-task pilot with researchers** (evaluation)

## v0.8 — Integrity and bias checks

Concrete, named checks rather than a general 'bias mitigator'. Each one reports evidence a reviewer can verify.

- **Retraction and correction notices from Crossref/Retraction Watch data** (integrity)
- **Reference-list profile: publication years, journals, self-citation** (integrity)
- **Screen journals against a predatory/hijacked journal list** (integrity)
- **Detect personal data in uploaded manuscripts** (integrity, backend)

## v1.0 — Multi-user deployment

Make it safe for a team or journal to use.

- **Authentication and roles (author, reviewer, admin)** (backend, frontend)
- **Reviewer independence: the uploader cannot sign off their own run** (backend)
- **Move checkpoints and run data to Postgres** (backend, infra)
- **Docker Compose: API, frontend and Ollama** (infra)
- **Export an audit bundle and publish its hash** (engine, backend)
