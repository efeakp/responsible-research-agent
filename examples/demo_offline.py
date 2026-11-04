"""Run the full LangGraph workflow against mocked scholarly APIs and a scripted
stand-in for Ollama — no network, no model. Pauses at human review, then signs off.
Usage: python examples/demo_offline.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from fixtures import make_transport, scripted_llm  # noqa: E402
from rra.pipeline import resume_run, start_run  # noqa: E402

transport = make_transport(scripted_llm)
result, run_dir = start_run(ROOT / "examples" / "sample_manuscript.md", ROOT / "runs",
                            ROOT / ".rra_demo_cache", mode="ollama",
                            ollama_host="http://localhost:11434", transport=transport)
print("Paused at:", result["status"], "->", run_dir / "report.html")
pending = result["__interrupt__"][0].value
print("Needs attention:", pending["references_needing_attention"], pending["claim_pairs_needing_attention"])

if "--signoff" in sys.argv:
    final = resume_run(run_dir, {"reviewer": "Demo reviewer", "decision": "approve_with_changes",
                                 "note": "Remove ref 3 and 4, fix ref 5, soften C2",
                                 "resolutions": [{"item": "C2/1", "verdict": "not_supported",
                                                  "note": "source does not claim every domain or 90%"}]},
                       cache_dir=ROOT / ".rra_demo_cache", transport=transport)
    print("Final:", final["status"])
