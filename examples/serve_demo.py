"""Start the API with mocked scholarly databases and a scripted stand-in for Ollama,
so you can try the web app with no network and no model.
Usage: python examples/serve_demo.py   (then run the frontend: cd frontend && npm run dev)
Upload examples/sample_manuscript.md — other files will mostly come back "not found"."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn  # noqa: E402
from fixtures import make_transport, scripted_llm  # noqa: E402
from rra.api import create_app  # noqa: E402

app = create_app(ROOT / "runs_demo", ROOT / ".rra_demo_cache", ollama_host="http://localhost:11434",
                 transport=make_transport(scripted_llm))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
