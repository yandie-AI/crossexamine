# crossexamine

**Your coding agent writes its own summary of what it did. Of course it looks fine.**

`crossexamine` puts the agent's **raw session transcript** in front of models from other vendors —
and lets them **veto the commit**.

A witness's own account of the testimony is not evidence. The transcript is.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## The problem

You let an AI agent work for an hour. At the end it tells you:

> ✅ Implemented the feature, added tests, all passing. Also fixed the quota gate.

Every word of that is written by the thing you are trying to audit. And the failure modes that
actually cost you are invisible in a summary, because a summary is where they go to hide:

- It said "verified" — and never ran a verification command.
- It saw a weird test result, decided it was flaky, and moved on.
- You corrected it. It agreed with you. It did not change what it does.
- It reported a number that no command in the session produced.
- It disabled its own guard to get past it, then reported success.

Reviewing the **diff** does not catch these. The diff is the one artifact the agent controls
completely. What it cannot edit is **the record of what it actually did**.

## What Crossexamine does

```
your agent works  ─▶  transcript (what it said / ran / saw)  ─▶  reviewers from other vendors
                                                                          │
                                    git commit  ◀── PASS ────────────────┤
                                    blocked     ◀── VETO ────────────────┘
```

1. **Slices the transcript** — verbatim: its words, its commands, the output it saw.
   Credentials masked. Mistakes kept, because mistakes are the evidence.
2. **Two-pass review.** A fast, high-temperature pass *nominates* faults. Then a
   **tool-equipped** pass verifies every nomination against the actual repository —
   keeping what holds up, killing what was invented, and adding what the first pass missed.
3. **Vetoes.** A veto blocks the commit. You can override it; the override goes on the record,
   written by the tool, not by you.

### Where the design comes from

The mechanism is older than software. In Tang-dynasty China the Chancellery (門下省) sat between the
body that drafted imperial edicts and the body that carried them out, and its defining power was not
to advise but to **refuse** — it could reject an edict outright and send it back to be rewritten.
Not because the emperor was foolish, but because *nobody* should be the sole reviewer of their own
decisions. That is the whole idea here, with an AI agent in the drafting seat.

The name is the courtroom version of the same thing: you do not cross-examine a witness by asking
for their summary. You cross-examine them **against the record**.

## Why two passes (this is the whole design)

The two channels you can reach a model through have opposite defects, and neither alone works:

| channel | temperature | tools | what goes wrong alone |
|---|---|---|---|
| direct API call | ✅ | ❌ | **invents evidence** |
| agent CLI (`claude -p`) | ❌ | ✅ | restates the author's framing at low temperature |

That first row is not hypothetical. On the very first run of this tool, a no-tools reviewer wrote:

> "crossexamine_guard.py source grep result: only a print statement, no Popen call"

It had no tools. It had never grepped anything. The `Popen` call was on line 216.
Two of its six findings were supported by citations it made up.

But here is the part that decided the design: **the problem it pointed at was real.** The
auto-dispatch path existed in the source and had never once been executed. High temperature
without tools finds real things and then fabricates the receipts. So: let it nominate freely,
then make something with tools go check. In the run right after that fix, the verifying pass
found a contradiction between a ledger entry and a design document — and quoted the file and
line number to prove it.

## Install

```bash
git clone https://github.com/yandie-AI/crossexamine && cd crossexamine && pip install -e .
```

> Not on PyPI yet — `pip install crossexamine` will work once the first tag is cut.
> Zero dependencies, so installing from source is a clone and a `pip install -e .`.

## Setup (about a minute)

Seats are configured through the environment. Any endpoint speaking the Anthropic Messages API
works — Anthropic itself, or the `/anthropic`-compatible endpoints most vendors now ship.

```bash
export CX_SEATS=minimax,deepseek,moonshot          # three vendors = real independence

export CX_MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic
export CX_MINIMAX_API_KEY=...
export CX_MINIMAX_MODEL=MiniMax-M2.7-highspeed
export CX_MINIMAX_TEMPERATURE=1.0                  # high: you want it hunting, not agreeing

export CX_DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
export CX_DEEPSEEK_API_KEY=...
export CX_DEEPSEEK_MODEL=deepseek-v4-pro

export CX_MOONSHOT_BASE_URL=https://api.kimi.com/coding
export CX_MOONSHOT_API_KEY=...
export CX_MOONSHOT_MODEL=kimi-k3
export CX_MOONSHOT_TEMPERATURE=off                 # some reasoning models reject temperature
```

Then:

```bash
crossexamine review                 # review what is staged
crossexamine install-hook           # or wire it into git so it is not optional
crossexamine status                 # reviews, vetoes, overrides, bypasses
```

## What it looks like

```
$ crossexamine review
crossexamine: routine change 27fb9cae4b1e -- 1 seat(s)
  CONCERN  solo/minimax           103s+165s -- 2 finding(s) [verified]

  [high] (kept) Ledger says T2a was moved to position 4, but the design doc still says position 1
      evidence: docs/.../order0_rules.md:67 "**Next (position 1)**: T2a ..." vs
                docs/audit/ledger.md:43 "T2a moved to position 4, paused"

  [low] (kept) The comment on crossexamine_guard.py:87 quotes the user, but the wording is the
      author's paraphrase, not what was actually said
      evidence: transcript [11:16:53] actual wording: "..."
```

That first finding is the shape of thing this tool is for. The agent had *recorded* the decision
in one place and never propagated it to the authoritative document — so both versions sat in the
repo contradicting each other. No diff review finds that. It needs someone who read the record,
then went and looked.

## Design decisions worth knowing about

**There is no exempt tier.** An earlier version waved through "small" changes, reasoning that
blocking typo fixes would just push the author into `--no-verify`. That was wrong. Small changes
are where the expensive mistakes live. Two real ones from the project this came from: a one-line
insertion into a table silently turned a three-reviewer panel into four reviewers with two on the
same vendor (fake independence, invisible); a stray `git add -A` swept 190 unrelated files into a
commit. Neither would have been reviewed. Routine change → one seat. Major change → three seats.
Nothing is skipped.

**The author never picks his own reviewer.** Seats rotate by a hash of the change. Otherwise you
can shop for the soft one — and you will, without noticing you are doing it.

**Reviewers read as widely as the agent, and write nothing.** `Read`, `Grep`, `Glob`, git history,
and an allow-list of read-only shell commands. No `Write`, no `Edit`. (Honest limit: this is a
narrowed tool surface, not a sandbox. Whether `git log; rm -rf /` gets split and judged depends on
your agent CLI's permission layer. For real isolation, run it in a read-only worktree.)

**A hang is reported as death, not as "still reviewing."** The first version launched the verifying
agent in the background and never watched it. It hung for 19 minutes — 6 seconds of CPU, zero child
processes, doing nothing at all — and the run looked like a review in progress. Anything that can
hang gets a watchdog and an honest verdict of *dead*.

**Overrides are recorded by the tool, not by you.** You keep the final say — a reviewer that cannot
be overridden gets routed around within a week. But every override lands in the ledger with your
stated reason, and so does every `CX_EMERGENCY` bypass. Bypassing is allowed. Bypassing
*quietly* is not.

**Stances, not checklists.** Each seat is told *where to stand*, never *what to look for*.
A checklist becomes a mechanical gate, and a mechanical gate can only catch the failure modes you
already thought of — which are, by construction, not the ones that will get you.

## Cost and latency

Measured, not estimated, on a ~110 KB transcript slice:

| pass | wall clock | notes |
|---|---|---|
| nomination (direct API, high temp) | 55–105 s | |
| verification (tool-equipped agent) | 140–170 s | 17 tool calls in the observed run |
| **routine change, one seat** | **~3–4 min** | |
| major change, three seats | ~4–5 min | seats run in parallel |

Whether that is worth it depends entirely on what a bad commit costs you. For a hobby project,
probably not. For an agent committing to something that matters, one review is cheaper than one
incident.

## Requirements

- Python ≥ 3.10, no dependencies
- An agent CLI for the verification pass (`claude` by default; `--agent` to change it)
- API access to at least one model; three different vendors if you want the full panel

## FAQ

**Does this work with agents other than Claude Code?**
The slicer currently understands Claude Code's transcript format
(`~/.claude/projects/*/*.jsonl`). The rest is format-agnostic. A slicer for another agent is
about 80 lines — PRs very welcome.

**Why not just use the same model to review itself?**
You can, and it is better than nothing. But a model reviewing its own output shares its blind
spots exactly. Different vendors, different training, different blind spots. That is the point
of insisting three seats land on three vendors.

**Isn't the transcript full of secrets?**
Credentials are masked before anything leaves your machine (see `slicer.REDACTIONS`). Everything
else — including your prompts and your agent's mistakes — is sent to whichever endpoints you
configured. If that is not acceptable, point the seats at a local model.

**What if the reviewer is just wrong?**
It often is, partly — that is why the second pass exists and why `killed` findings stay visible
in the output. You hold the final say. Override it; the override is on the record.

## License

MIT — see [LICENSE](LICENSE).

Extracted from the governance layer of an internal AI engineering platform, where it reviews an
AI agent that has commit access. Its first real veto found a mistake the agent had made
that same hour.
