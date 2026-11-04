"""Append-only, hash-chained audit log.

Each line is a JSON event carrying the SHA-256 of the previous line, so any
edit, deletion or reordering after the fact breaks the chain and is detected
by `verify_chain`. This is tamper-evident, not tamper-proof: someone could
rewrite the whole file. For stronger guarantees, publish the final hash
(it is printed at the end of every run) somewhere independent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(s: str | bytes) -> str:
    if isinstance(s, str):
        s = s.encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev_hash = GENESIS
        self.seq = 0
        if self.path.exists():
            # resume an existing chain (used by sign-off)
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ev = json.loads(line)
                    self.prev_hash = ev["hash"]
                    self.seq = ev["seq"] + 1

    def log(self, event: str, **data: Any) -> dict:
        entry = {"seq": self.seq, "ts": utcnow(), "event": event,
                 "data": data, "prev_hash": self.prev_hash}
        entry["hash"] = sha256_text(_canonical(entry))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(_canonical(entry) + "\n")
        self.prev_hash = entry["hash"]
        self.seq += 1
        return entry


def verify_chain(path: Path) -> tuple[bool, str]:
    prev = GENESIS
    lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, line in enumerate(lines):
        entry = json.loads(line)
        claimed = entry.pop("hash", None)
        if entry.get("seq") != i:
            return False, f"line {i + 1}: sequence number {entry.get('seq')} out of order"
        if entry.get("prev_hash") != prev:
            return False, f"line {i + 1}: prev_hash does not match previous entry"
        if sha256_text(_canonical(entry)) != claimed:
            return False, f"line {i + 1}: content hash mismatch (entry was edited)"
        prev = claimed
    return True, f"{len(lines)} entries intact; final hash {prev}"
