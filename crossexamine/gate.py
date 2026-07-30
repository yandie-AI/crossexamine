"""The gate: tiering, the commit hook, and the append-only ledger.

Design note that matters more than it looks
-------------------------------------------
An earlier version of this had an **exempt tier** for "small" changes -- doc typos, one-liners --
on the theory that blocking those would push the author into `--no-verify` and defeat the gate.

That reasoning was wrong, and wrong in an instructive way: small changes are exactly where the
expensive mistakes live. Real examples from the project this was extracted from: a one-line
insertion into a table silently turned a three-reviewer panel into four reviewers with two on
the same vendor (fake independence); a stray `git add -A` swept 190 unrelated files into a commit.
Neither would have been reviewed under an exempt tier.

So: **there is no exempt tier.** Routine change, one seat. Major change, three seats.
The escape hatch (`CX_EMERGENCY`) exists, but it writes itself to the ledger -- bypassing is
allowed, bypassing *quietly* is not.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

#: Paths whose change alters *the basis on which future decisions are made*.
#: Override with a `crossexamine.toml`/env in your own repo; these are sensible defaults.
MAJOR_PATTERNS: tuple[str, ...] = (
    r"^(CLAUDE|AGENTS|README)\.md$",
    r"^docs/(design|architecture|adr)/",
    r"^\.github/workflows/",
    r"(^|/)(pyproject\.toml|package\.json|Cargo\.toml)$",
)


def staged_files(repo: Path) -> list[str]:
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         cwd=str(repo), capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def is_major(paths: list[str], patterns: tuple[str, ...] = MAJOR_PATTERNS) -> tuple[bool, list[str]]:
    hits = [p for p in paths if any(re.search(rx, p) for rx in patterns)]
    return bool(hits), hits


def change_signature(repo: Path, paths: list[str]) -> str:
    """Signature over **content**, not filenames.

    Same files with different content means a different signature, which means it must be
    reviewed again. Prevents "reviewed once, waved through forever".
    """
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(path.encode())
        h.update(subprocess.run(["git", "show", f":{path}"], cwd=str(repo),
                                capture_output=True).stdout or b"")
    return h.hexdigest()[:16]


def staged_diff(repo: Path, limit: int = 60000) -> str:
    out = subprocess.run(["git", "diff", "--cached"], cwd=str(repo),
                         capture_output=True, text=True).stdout
    if not out.strip():
        out = subprocess.run(["git", "diff"], cwd=str(repo),
                             capture_output=True, text=True).stdout
    return out[:limit] if len(out) > limit else (out or "(no changes)")


class Ledger:
    """Append-only record of every review, every override, every bypass.

    The point is not bookkeeping. It is that **overriding the reviewers leaves a trace you
    did not write yourself** -- which is the only thing that makes "the author keeps final say"
    survivable as a design.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def unanswered_vetoes(self) -> list[dict]:
        answered = {e.get("signature") for e in self.entries() if e.get("event") == "answered"}
        return [e for e in self.entries()
                if e.get("event") == "review" and e.get("verdict") == "VETO"
                and e.get("signature") not in answered]


HOOK_TEMPLATE = """#!/usr/bin/env bash
# installed by `crossexamine install-hook`
exec {python} -m crossexamine.cli precommit
"""


def install_hook(repo: Path, python: str = "python3") -> Path:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    existing = hook.read_text(encoding="utf-8") if hook.exists() else ""
    if "crossexamine.cli precommit" in existing:
        return hook
    if existing.strip():                       # keep whatever is already there
        hook.write_text(existing.rstrip("\n") + f"\n\n{python} -m crossexamine.cli precommit\n",
                        encoding="utf-8")
    else:
        hook.write_text(HOOK_TEMPLATE.format(python=python), encoding="utf-8")
    hook.chmod(0o755)
    return hook


def emergency_reason() -> str:
    return os.environ.get("CX_EMERGENCY", "").strip()
