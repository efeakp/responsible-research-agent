"""Convert an article to verifier-ready Markdown. Same as `rra convert`.

    python scripts/to_markdown.py paper.pdf               # writes paper.md
    python scripts/to_markdown.py paper.docx -o out.md
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rra.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["convert", *sys.argv[1:]]))
