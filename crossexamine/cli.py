"""`crossexamine` command line."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from crossexamine import council, gate, prompts, slicer

WORKDIR = Path(os.environ.get("CX_HOME", Path.home() / ".crossexamine"))


def load_seats() -> list[council.Seat]:
    """Seats come from env: `CX_SEATS=minimax,deepseek,moonshot` plus per-seat vars.

    Deliberately not a config file: the API keys have to come from the environment anyway,
    and one source beats two that can disagree.
    """
    names = [s.strip().upper() for s in os.environ.get("CX_SEATS", "").split(",") if s.strip()]
    seats = [council.Seat.from_env(n) for n in names]
    return [s for s in seats if s.base_url and s.api_key and s.model]


def do_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    paths = gate.staged_files(repo) or ([args.file] if args.file else [])
    if not paths:
        print("crossexamine: nothing staged to review")
        return 0
    major, hits = gate.is_major(paths)
    if args.major:
        major, hits = True, paths[:5]
    sig = gate.change_signature(repo, paths)
    seats = load_seats()
    if not seats:
        print("crossexamine: no seats configured. See README (CX_SEATS).", file=sys.stderr)
        return 2

    work = WORKDIR / sig
    work.mkdir(parents=True, exist_ok=True)
    diff = gate.staged_diff(repo)
    record = work / "record.md"
    slicer.write_slice(record, since_minutes=args.since_minutes, last_n=args.last_n)
    self_report = Path(args.self_report).read_text(encoding="utf-8") if args.self_report else \
        "(the agent filed no account of this change)"

    assigned = council.assign_seats(sig, seats, major)
    print(f"crossexamine: {'MAJOR' if major else 'routine'} change {sig} "
          f"-- {len(assigned)} seat(s){' -- ' + ', '.join(hits[:3]) if hits else ''}")

    verdicts = []
    record_text = record.read_text(encoding="utf-8")
    for position, seat in assigned:
        stance = council.STANCES[position]
        v = council.Verdict(seat=f"{position}/{seat.name}")
        try:
            nominations, v.elapsed_nominate = council.nominate(
                seat, prompts.nominate_prompt(stance=stance, record=record_text[:110000],
                                              self_report=self_report, diff=diff))
        except Exception as exc:                                  # noqa: BLE001
            v.error = f"nomination failed: {type(exc).__name__}: {exc}"
            verdicts.append(v)
            print(f"  DEAD  {v.seat}: {v.error}")
            continue
        (work / f"nominations_{position}.md").write_text(nominations, encoding="utf-8")

        if args.no_verify:
            parsed, v.verified = council.extract_json(nominations), False
        else:
            tb = work / f"verify_{position}.md"
            tb.write_text(prompts.verify_prompt(
                stance=stance, record_path=str(record),
                self_report_path=args.self_report or "(none)", nominations=nominations),
                encoding="utf-8")
            text, v.elapsed_verify, err = council.verify(
                tb, work / f"agent_{position}.json", agent_cmd=args.agent,
                model=os.environ.get("CX_VERIFY_MODEL", seat.model),
                base_url=os.environ.get("CX_VERIFY_BASE_URL", seat.base_url),
                api_key=os.environ.get("CX_VERIFY_API_KEY", seat.api_key),
                timeout=args.timeout)
            if text:
                (work / f"verified_{position}.md").write_text(text, encoding="utf-8")
            parsed, v.verified = (council.extract_json(text), True) if text else \
                (council.extract_json(nominations), False)
            v.error = err

        if parsed:
            v.verdict = parsed.get("verdict", "ABSENT")
            v.frame_wrong = bool(parsed.get("frame_wrong"))
            v.reasons = parsed.get("reasons", []) or []
            v.note = parsed.get("note", "")
            if not v.verified:
                v.note = (f"UNVERIFIED (verification failed: {v.error}) -- evidence below was "
                          f"not checked against the repo and may be fabricated. {v.note}")
        verdicts.append(v)
        flag = "verified" if v.verified else "UNVERIFIED"
        print(f"  {v.verdict:<8} {v.seat:<22} {v.elapsed_nominate:.0f}s+{v.elapsed_verify:.0f}s "
              f"-- {len(v.reasons)} finding(s) [{flag}]")

    (work / "verdicts.json").write_text(
        json.dumps([v.to_dict() for v in verdicts], ensure_ascii=False, indent=2), encoding="utf-8")
    ledger = gate.Ledger(WORKDIR / "ledger.jsonl")
    vetoed = [v for v in verdicts if v.verdict == "VETO"] or \
             ([v for v in verdicts if v.frame_wrong] if
              len([v for v in verdicts if v.frame_wrong]) >= 2 else [])
    ledger.append({"event": "review", "signature": sig, "major": major,
                   "seats": [v.seat for v in verdicts],
                   "verdict": "VETO" if vetoed else ("CONCERN" if any(
                       v.verdict == "CONCERN" for v in verdicts) else "PASS"),
                   "findings": sum(len(v.reasons) for v in verdicts),
                   "verified": all(v.verified for v in verdicts)})

    for v in verdicts:
        for r in v.reasons:
            print(f"\n  [{r.get('severity', '?')}] ({r.get('source', '-')}) {r.get('claim', '')}")
            print(f"      evidence: {str(r.get('evidence', ''))[:300]}")
    if any(v.note for v in verdicts):
        print("\n  " + "\n  ".join(v.note for v in verdicts if v.note))
    print(f"\n  full output: {work}")
    return 1 if vetoed else 0


def do_precommit(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    paths = gate.staged_files(repo)
    if not paths:
        return 0
    ledger = gate.Ledger(WORKDIR / "ledger.jsonl")
    sig = gate.change_signature(repo, paths)

    if (why := gate.emergency_reason()):
        ledger.append({"event": "bypass", "signature": sig, "reason": why})
        print(f"crossexamine: emergency bypass recorded in the ledger -- {why}")
        return 0
    if unanswered := ledger.unanswered_vetoes():
        print(f"crossexamine: {len(unanswered)} unanswered VETO(s). A veto is a remand, not a suggestion.")
        print("  answer with: crossexamine answer <signature> --why '...'")
        return 1
    if any(e.get("signature") == sig and e.get("event") == "signed" for e in ledger.entries()):
        return 0
    print(f"crossexamine: change {sig} has not been reviewed. Run `crossexamine review`, "
          f"or set CX_EMERGENCY='reason' (recorded in the ledger).")
    return 1


def do_answer(args: argparse.Namespace) -> int:
    ledger = gate.Ledger(WORKDIR / "ledger.jsonl")
    ledger.append({"event": "answered", "signature": args.signature,
                   "decision": args.decision, "why": args.why})
    ledger.append({"event": "signed", "signature": args.signature})
    print(f"crossexamine: {args.signature} answered ({args.decision}) and recorded.")
    return 0


def do_status(args: argparse.Namespace) -> int:
    ledger = gate.Ledger(WORKDIR / "ledger.jsonl")
    entries = ledger.entries()
    reviews = [e for e in entries if e.get("event") == "review"]
    print(f"reviews: {len(reviews)} | vetoes: {sum(1 for e in reviews if e['verdict'] == 'VETO')}"
          f" | overridden: {sum(1 for e in entries if e.get('decision') == 'override')}"
          f" | bypasses: {sum(1 for e in entries if e.get('event') == 'bypass')}")
    for e in ledger.unanswered_vetoes():
        print(f"  UNANSWERED VETO  {e['signature']}  {e.get('ts', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crossexamine", description=(
        "Adversarial review for AI coding agents: other models read your agent's actual "
        "transcript -- not its self-report -- and can veto the commit."))
    ap.add_argument("--repo", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="review the staged change")
    r.add_argument("--file", help="review a specific file instead of the staged set")
    r.add_argument("--self-report", help="optional: the agent's own account of the change")
    r.add_argument("--major", action="store_true", help="force the three-seat panel")
    r.add_argument("--no-verify", action="store_true",
                   help="skip the tool-equipped pass (findings will be flagged UNVERIFIED)")
    r.add_argument("--since-minutes", type=int, default=120)
    r.add_argument("--last-n", type=int, default=0)
    r.add_argument("--agent", default="claude", help="agent CLI used for the verification pass")
    r.add_argument("--timeout", type=int, default=council.DEFAULT_TIMEOUT)
    r.set_defaults(fn=do_review)

    p = sub.add_parser("precommit", help="gate used by the git hook")
    p.set_defaults(fn=do_precommit)

    a = sub.add_parser("answer", help="answer a veto and sign the change off")
    a.add_argument("signature")
    a.add_argument("--decision", choices=["accept", "override"], default="accept")
    a.add_argument("--why", default="", help="required when overriding the reviewers")
    a.set_defaults(fn=do_answer)

    s = sub.add_parser("status", help="ledger summary")
    s.set_defaults(fn=do_status)

    h = sub.add_parser("install-hook", help="install the pre-commit hook")
    h.set_defaults(fn=lambda args: (print(f"installed: "
                                          f"{gate.install_hook(Path(args.repo).resolve())}"), 0)[1])

    args = ap.parse_args(argv)
    if getattr(args, "decision", None) == "override" and not getattr(args, "why", ""):
        print("crossexamine: overriding the reviewers requires --why. The reason goes on the record; "
              "that record is what makes 'the author keeps final say' survivable.", file=sys.stderr)
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
