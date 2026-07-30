"""Extract what your agent *actually did* from its session transcript.

The point of this module: an agent's self-report is written by the thing being audited.
Reviewers must read the raw record instead — what it said, what it ran, what it saw.

Three disciplines, deliberately:

1. **No summarizing.** Verbatim excerpts only. The moment you summarize, you are back to
   reading the agent's account of itself.
2. **Truncate, never filter.** Long tool output gets head+tail truncation. It is never
   dropped "because it looked unimportant" — deciding what matters is the reviewer's job.
3. **Redact credentials only.** Keys and tokens are masked. Errors, contradictions and
   embarrassing moments are kept verbatim. Those are the evidence.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Credential patterns. Note what is absent: nothing here removes errors or mistakes.
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]+"), r"\1...REDACTED"),
    (re.compile(r"(eyJ[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-.]+"), r"\1...REDACTED"),
    (re.compile(r"((?:API_KEY|TOKEN|PASSWORD|SECRET|AUTH)\w*\s*[=:]\s*)\S{8,}", re.I),
     r"\1...REDACTED"),
    (re.compile(r"(gh[pousr]_)[A-Za-z0-9]{16,}"), r"\1...REDACTED"),
]

TOOL_HEAD, TOOL_TAIL, TEXT_MAX = 1200, 400, 4000


def redact(s: str) -> str:
    for rx, rep in REDACTIONS:
        s = rx.sub(rep, s)
    return s


def default_transcript_dir(cwd: Path | None = None) -> Path:
    """Claude Code stores transcripts under ~/.claude/projects/<path-with-dashes>/."""
    cwd = (cwd or Path.cwd()).resolve()
    return Path.home() / ".claude" / "projects" / str(cwd).replace("/", "-")


def latest_transcript(directory: Path | None = None) -> Path | None:
    directory = directory or default_transcript_dir()
    if not directory.exists():
        return None
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _blocks(entry: dict) -> list[dict]:
    msg = entry.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def slice_transcript(path: Path, *, since_minutes: int = 0, last_n: int = 0) -> list[str]:
    """Return verbatim segments: agent speech, tool calls, tool results, user messages."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)) if since_minutes else None
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") not in ("assistant", "user"):
                continue
            if since:
                try:
                    ts = datetime.fromisoformat((entry.get("timestamp") or "").replace("Z", "+00:00"))
                    if ts < since:
                        continue
                except ValueError:
                    pass
            rows.append(entry)
    if last_n:
        rows = rows[-last_n:]

    out: list[str] = []
    for entry in rows:
        who = "AGENT" if entry.get("type") == "assistant" else "USER"
        ts = (entry.get("timestamp") or "")[11:19]
        for block in _blocks(entry):
            kind = block.get("type")
            if kind == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append(f"\n### [{ts}] {who} said\n{redact(text[:TEXT_MAX])}")
            elif kind == "tool_use":
                payload = json.dumps(block.get("input") or {}, ensure_ascii=False)
                out.append(f"\n### [{ts}] AGENT ran `{block.get('name', '?')}`\n"
                           f"```\n{redact(payload[:TOOL_HEAD])}\n```")
            elif kind == "tool_result":
                content = block.get("content")
                body = redact(content if isinstance(content, str)
                              else json.dumps(content, ensure_ascii=False) or "")
                if len(body) > TOOL_HEAD + TOOL_TAIL:
                    dropped = len(body) - TOOL_HEAD - TOOL_TAIL
                    body = (body[:TOOL_HEAD]
                            + f"\n...[{dropped} chars truncated -- truncation, not selection]...\n"
                            + body[-TOOL_TAIL:])
                out.append(f"\n### [{ts}] AGENT saw\n```\n{body}\n```")
    return out


def write_slice(out: Path, *, transcript: Path | None = None, since_minutes: int = 120,
                last_n: int = 0, diff: str = "") -> Path:
    transcript = transcript or latest_transcript()
    if transcript is None:
        raise FileNotFoundError("no transcript found; pass --transcript explicitly")
    segments = slice_transcript(transcript, since_minutes=since_minutes, last_n=last_n)
    header = f"""# What the agent actually thought and did

> This is **not** the agent's summary of its own work. It is the raw record:
> what it said, what commands it ran, what output it saw.
>
> **How to use it:** compare *what it claimed* against *what it did*. The gap is your finding.
> Things worth checking: claims of "verified" with no verification command in the record;
> anomalous output that it walked past; corrections from the user that never changed behaviour;
> numbers reported with no command that produced them.
>
> **Slicing discipline:** verbatim, no summarizing. Long output is head/tail truncated,
> never filtered by importance. Credentials are masked -- **mistakes are not**.
>
> Source: `{transcript.name}` - {len(segments)} segments

---
"""
    if diff:
        header += f"\n## Changes under review\n\n```diff\n{diff[:60000]}\n```\n\n---\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + "\n## Raw record\n" + "".join(segments), encoding="utf-8")
    return out
