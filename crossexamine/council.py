"""The council: seats, stances, and the two-pass review.

Why two passes
--------------
Neither channel alone is good enough, and the reason is mechanical, not philosophical:

======================  ==========  =========  ====================================
channel                 temperature tools      failure mode when used alone
======================  ==========  =========  ====================================
direct API call         yes         no         **invents evidence.** Observed: a seat
                                               wrote "I grepped the source and found..."
                                               with no tools available to it at all.
agent CLI (claude -p)   no          yes        low-temperature restatement of the
                                               author's own framing -- the exact thing
                                               a reviewer is supposed to break.
======================  ==========  =========  ====================================

So: pass 1 nominates faults at high temperature (fast, divergent, evidence unreliable);
pass 2 gives a tool-equipped agent those nominations and makes it **verify each one against
the actual repository**, kill the fabricated ones, and add whatever pass 1 missed.
Pass 2's output is the verdict. If pass 2 dies, pass 1's findings are reported but explicitly
flagged UNVERIFIED -- never silently promoted.

Permissions
-----------
The council reads as widely as the agent it reviews, and **writes nothing**. See READ_ONLY_TOOLS.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: Read access comparable to the agent under review; **no write tools at all**.
#: Bash is allow-listed per command rather than opened wholesale, because an open Bash
#: can redirect and delete. Honest limitation: whether compound commands
#: (`git log; rm -rf /`) are split and judged depends on your agent CLI's permission layer.
#: This is a *narrowed* tool surface, not a sandbox. For real isolation, run in a
#: read-only worktree or container.
READ_ONLY_TOOLS = ",".join((
    "Read", "Grep", "Glob",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)",
    "Bash(git blame:*)", "Bash(git ls-files:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
    "Bash(stat:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(find:*)", "Bash(diff:*)",
    "Bash(sed -n:*)", "Bash(file:*)", "Bash(realpath:*)",
))

#: Review positions. These are **stances, not checklists**.
#: A checklist degrades into a mechanical gate, and a mechanical gate cannot enumerate
#: the ways an agent goes wrong -- that is the whole reason a model sits in this seat.
STANCES: dict[str, str] = {
    "presence": (
        "Review from the position of **factual presence**: the things this change claims "
        "exist -- machines, fields, tests, prior conclusions -- do they actually exist? "
        "You have read-only tools. Go check. Do not limit yourself to this one question; "
        "anything you see from here is fair game."),
    "coherence": (
        "Review from the position of **internal and historical consistency**: does this change "
        "contradict its own other parts, the project's stated principles, or mechanisms already "
        "in the repo? Has the author reversed himself without noticing?"),
    "framing": (
        "Review from the position of **the question itself**: is this change even asking the right "
        "question? **You have the authority to rule that the change asks the wrong question and to "
        "veto on that ground alone** -- this is the primary reason this seat exists. The author's "
        "most expensive mistakes come from treating what he already thought of as the boundary. "
        "You are not bound by his framing."),
    "solo": (
        "You are the **only** seat on this review. Work through all three positions in turn: "
        "(1) factual presence -- does what he claims exist actually exist? you have tools, go check; "
        "(2) consistency -- does this contradict his own other parts, the project's principles, or "
        "existing mechanisms? (3) the question itself -- is he even asking the right question? "
        "**You may veto on the ground that the question is wrong.** "
        "One seat means thinner coverage than three: say plainly at the end which position you did "
        "not get to. Do not pretend you covered all three."),
}

#: Three seats => three different vendors. Same-family seats are fake independence.
TRIO: tuple[str, ...] = ("presence", "coherence", "framing")

DEFAULT_TIMEOUT = 480


@dataclass
class Seat:
    """One reviewing seat. `base_url` must speak the Anthropic Messages API.

    Works with Anthropic proper and with the Anthropic-compatible endpoints most
    vendors now ship (MiniMax `/anthropic`, DeepSeek `/anthropic`, Moonshot `/coding`, ...).
    """
    name: str
    base_url: str
    api_key: str
    model: str
    temperature: float | None = 1.0        # None => omit (some reasoning models reject it)

    @classmethod
    def from_env(cls, prefix: str) -> "Seat":
        """Build from `CX_<PREFIX>_{BASE_URL,API_KEY,MODEL,TEMPERATURE}`."""
        g = lambda k, d="": os.environ.get(f"CX_{prefix}_{k}", d)  # noqa: E731
        temp = g("TEMPERATURE", "1.0")
        return cls(name=prefix.lower(), base_url=g("BASE_URL").rstrip("/"),
                   api_key=g("API_KEY"), model=g("MODEL"),
                   temperature=None if temp.lower() in ("", "none", "off") else float(temp))


@dataclass
class Verdict:
    verdict: str = "ABSENT"
    frame_wrong: bool = False
    reasons: list[dict] = field(default_factory=list)
    note: str = ""
    verified: bool = False
    seat: str = ""
    elapsed_nominate: float = 0.0
    elapsed_verify: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "frame_wrong": self.frame_wrong,
                "reasons": self.reasons, "note": self.note, "verified": self.verified,
                "seat": self.seat, "elapsed_nominate": round(self.elapsed_nominate, 1),
                "elapsed_verify": round(self.elapsed_verify, 1), "error": self.error}


def assign_seats(change_id: str, seats: list[Seat], major: bool) -> list[tuple[str, Seat]]:
    """Rotate seats by a hash of the change -- **the author never picks his own reviewer**.

    Routine change => one seat. Major change => three seats on three different vendors.
    There is no exempt tier: small edits are where the expensive mistakes live.
    """
    if not seats:
        raise ValueError("no seats configured (see README: CX_SEATS)")
    off = int(change_id[:8], 16) % len(seats)
    if major:
        return [(pos, seats[(off + i) % len(seats)]) for i, pos in enumerate(TRIO)]
    return [("solo", seats[off])]


def _messages_api(seat: Seat, prompt: str, *, max_tokens: int = 8000, timeout: int = 300) -> str:
    body: dict = {"model": seat.model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": prompt}]}
    if seat.temperature is not None:
        body["temperature"] = seat.temperature
    req = urllib.request.Request(
        f"{seat.base_url}/v1/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                 "x-api-key": seat.api_key, "authorization": f"Bearer {seat.api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return "".join(b.get("text", "") for b in payload.get("content", []) if isinstance(b, dict))


def extract_json(text: str) -> dict | None:
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _agent_result_text(raw: str) -> str:
    """Pull the assistant's answer out of `claude -p --output-format json` output.

    The file's first line is often a stderr banner, so parse the **last** line that starts
    with `{` and has a `result` field. Getting this wrong is quietly catastrophic: you end up
    parsing the *envelope*, the verdict comes back empty, and the run looks like it reviewed
    something when it read nothing.
    """
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and "result" in obj:
                return obj.get("result") or ""
    return raw


def nominate(seat: Seat, prompt: str) -> tuple[str, float]:
    """Pass 1 -- high temperature, no tools. Optimised for *finding* faults, not proving them."""
    t0 = time.time()
    return _messages_api(seat, prompt), time.time() - t0


def verify(taskbook: Path, out_json: Path, *, agent_cmd: str = "claude",
           model: str = "", base_url: str = "", api_key: str = "",
           timeout: int = DEFAULT_TIMEOUT, max_turns: int = 40) -> tuple[str, float, str]:
    """Pass 2 -- tool-equipped agent verifies the nominations against the real repo.

    Returns (result_text, elapsed, error). Includes a watchdog: an agent that hangs is
    reported as dead, never as "still reviewing". A hang that nobody watches is
    indistinguishable from a review that never happened.
    """
    t0 = time.time()
    env = {**os.environ}
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env.pop("ANTHROPIC_API_KEY", None)   # its presence hijacks OAuth detection in some CLIs
    if model:
        for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[var] = model
    cmd = [agent_cmd, "-p", taskbook.read_text(encoding="utf-8"),
           "--output-format", "json", "--permission-mode", "acceptEdits",
           "--allowedTools", READ_ONLY_TOOLS, "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "", time.time() - t0, f"verification agent exceeded {timeout}s (counted as dead)"
    except FileNotFoundError:
        return "", time.time() - t0, f"agent command not found: {agent_cmd}"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(proc.stdout or proc.stderr, encoding="utf-8")
    text = _agent_result_text(proc.stdout or "")
    return text, time.time() - t0, "" if text else (proc.stderr or "empty output")[:200]
