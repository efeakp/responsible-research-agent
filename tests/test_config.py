import os
import subprocess
from pathlib import Path

from rra.config import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def test_reads_values_comments_quotes_and_bom(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY", "E_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("﻿# comment\n\nA_KEY=abc123\nexport B_KEY='quoted value'\nC_KEY=\"x # not a comment\"\n"
                   "D_KEY=value # trailing comment\nnot a setting\nE_KEY=\n", encoding="utf-8")
    loaded = load_dotenv(env)
    assert loaded == ["A_KEY", "B_KEY", "C_KEY", "D_KEY", "E_KEY"]
    assert os.environ["A_KEY"] == "abc123" and os.environ["B_KEY"] == "quoted value"
    assert os.environ["C_KEY"] == "x # not a comment" and os.environ["D_KEY"] == "value"
    assert os.environ["E_KEY"] == ""


def test_terminal_setting_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "from-terminal")
    env = tmp_path / ".env"
    env.write_text("OPENALEX_API_KEY=from-file\n", encoding="utf-8")
    assert load_dotenv(env) == []
    assert os.environ["OPENALEX_API_KEY"] == "from-terminal"


def test_missing_file_is_fine(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == []


def test_env_file_is_ignored_by_git_but_example_is_not(tmp_path):
    import shutil
    import pytest
    if not shutil.which("git"):
        pytest.skip("git not installed")
    rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in rules and "!.env.example" in rules
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    ignored = subprocess.run(["git", "check-ignore", ".env", ".env.local", ".env.example"], cwd=repo,
                             capture_output=True, text=True).stdout.split()
    assert ignored == [".env", ".env.local"]
