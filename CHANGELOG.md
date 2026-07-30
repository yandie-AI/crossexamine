# Changelog

## 0.1.0 — 2026-07-30

First release.

- **Transcript slicer** — extracts what the agent actually said, ran, and saw from its session
  record. Verbatim; long output is head/tail truncated rather than filtered by importance;
  credentials are masked and mistakes are not.
- **Two-pass council** — a high-temperature pass with no tools nominates faults, then a
  tool-equipped pass verifies each nomination against the real repository, kills the invented
  ones, and adds what the first pass missed.
- **Read-only reviewers** — `Read`/`Grep`/`Glob`, git history and an allow-list of read-only
  shell commands. No `Write`, no `Edit`.
- **Gate** — content-based change signatures, no exempt tier, seats rotated by hash so the author
  cannot pick a soft reviewer, append-only ledger, `pre-commit` hook.
- Zero dependencies. Python ≥ 3.10.
