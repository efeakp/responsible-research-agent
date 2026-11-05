"""Settings from a .env file in the folder you run rra from.

A small built-in reader (no extra dependency). Lines are KEY=VALUE; blank lines and
lines starting with # are ignored; values may be quoted. A variable already set in the
environment (e.g. with `set` in the terminal) wins over the file.

.env is listed in .gitignore. Never commit it: copy .env.example instead.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path | str = ".env") -> list[str]:
    """Load KEY=VALUE pairs into os.environ without overriding existing values.
    Returns the names loaded (never the values)."""
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    # utf-8-sig: Windows Notepad may add a byte-order mark
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()      # allow trailing comments
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
