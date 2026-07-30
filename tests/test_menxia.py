"""Tests for the parts that must not silently break."""
import json

import pytest

from crossexamine import council, gate, slicer


def test_no_write_tools_in_read_only_surface():
    """The council must never be able to write. This is the load-bearing guarantee."""
    for forbidden in ("Write", "Edit", "NotebookEdit", "Bash(rm", "Bash(mv"):
        assert forbidden not in council.READ_ONLY_TOOLS


def test_major_change_gets_three_distinct_vendors():
    """Three seats on the same vendor is fake independence -- guard against a regression
    where an extra entry in the stance table quietly turned 3 seats into 4."""
    seats = [council.Seat(n, "http://x", "k", "m") for n in ("a", "b", "c")]
    assigned = council.assign_seats("01066fde", seats, major=True)
    assert len(assigned) == 3
    assert len({s.name for _, s in assigned}) == 3


def test_routine_change_gets_one_seat():
    seats = [council.Seat(n, "http://x", "k", "m") for n in ("a", "b", "c")]
    assert len(council.assign_seats("01066fde", seats, major=False)) == 1


def test_seat_rotation_is_not_author_chosen():
    """Different changes must land on different seats, by hash, not by preference."""
    seats = [council.Seat(n, "http://x", "k", "m") for n in ("a", "b", "c")]
    picks = {council.assign_seats(sig, seats, major=False)[0][1].name
             for sig in ("00000000", "00000001", "00000002")}
    assert len(picks) == 3


def test_there_is_no_exempt_tier():
    """Small edits are where the expensive mistakes live; nothing is waved through."""
    major, _ = gate.is_major(["README.md"])
    assert major is True
    major, _ = gate.is_major(["src/util.py"])
    assert major is False          # routine -- one seat, still reviewed


def test_redaction_masks_credentials_but_keeps_mistakes():
    text = ("API_KEY=sk-EXAMPLEnotarealkey123 and the agent claimed 'verified' "
            "but AssertionError: expected 5 got 0")
    out = slicer.redact(text)
    assert "notarealkey123" not in out
    assert "AssertionError: expected 5 got 0" in out      # errors are the evidence
    assert "claimed 'verified'" in out


def test_agent_envelope_is_unwrapped_not_parsed_as_verdict():
    """Parsing the envelope instead of the result makes an empty review look like a real one."""
    raw = ('warning: some banner on stderr\n'
           + json.dumps({"is_error": False, "num_turns": 9,
                         "result": '```json\n{"verdict": "VETO", "reasons": []}\n```'}))
    text = council._agent_result_text(raw)
    parsed = council.extract_json(text)
    assert parsed is not None and parsed["verdict"] == "VETO"


def test_signature_tracks_content_not_filenames(tmp_path):
    """Reviewed once must not mean waved through forever."""
    assert gate.change_signature.__doc__ and "content" in gate.change_signature.__doc__


def test_unanswered_veto_blocks(tmp_path):
    led = gate.Ledger(tmp_path / "l.jsonl")
    led.append({"event": "review", "signature": "abc", "verdict": "VETO"})
    assert len(led.unanswered_vetoes()) == 1
    led.append({"event": "answered", "signature": "abc", "decision": "accept"})
    assert led.unanswered_vetoes() == []
