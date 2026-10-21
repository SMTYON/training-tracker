#!/usr/bin/env python3
"""Offline tests with fake data. Run: python test_engine.py"""

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import engine

UTC = timezone.utc


def week(wid, start, end, problems, threshold=0.5, **extra):
    return {"id": wid, "name": wid, "start": start, "end": end,
            "threshold": threshold, "problems": problems, **extra}


TRAINEES = [
    {"username": "ann", "nickname": "Ann", "email": "ann@x.com"},
    {"username": "bob", "nickname": "Bob", "email": "bob@x.com"},
    {"username": "cat", "nickname": "Cat", "email": "cat@x.com"},
]


def test_ranking_and_window():
    w = week("w1", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z", ["a", "b", "c", "d"], 0.5)
    history = {
        # Ann: 3 solved, fast
        "ann": {"a": ["2026-10-01T01:00:00+00:00"], "b": ["2026-10-01T02:00:00+00:00"],
                "c": ["2026-10-01T03:00:00+00:00"]},
        # Bob: 3 solved, slower -> ranks below Ann
        "bob": {"a": ["2026-10-02T00:00:00+00:00"], "b": ["2026-10-02T01:00:00+00:00"],
                "c": ["2026-10-02T02:00:00+00:00"]},
        # Cat: solved "a" BEFORE the week (does not count), "b" after the end (does not count),
        # "c" once early and again later (earliest in-window counts)
        "cat": {"a": ["2026-09-30T23:00:00+00:00"], "b": ["2026-10-09T00:00:00+00:00"],
                "c": ["2026-10-03T00:00:00+00:00", "2026-10-01T05:00:00+00:00"]},
    }
    rows = engine.score_week(w, TRAINEES, history, {})
    by = {r["username"]: r for r in rows}
    assert [r["username"] for r in rows] == ["ann", "bob", "cat"], rows
    assert by["ann"]["solved"] == 3 and by["ann"]["time_minutes"] == 60 + 120 + 180
    assert by["bob"]["rank"] == 2
    assert by["cat"]["solved"] == 1 and by["cat"]["time_minutes"] == 300  # 05:00 not 03 Oct
    assert by["cat"]["per_problem"] == [None, None, 300, None]
    assert by["ann"]["passed"] and by["bob"]["passed"] and not by["cat"]["passed"]


def test_threshold_rounding():
    assert engine.required_solves(week("w", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z",
                                       list("abcdef"), 0.65)) == 4   # 3.9 -> 4
    assert engine.required_solves(week("w", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z",
                                       list("abcdefghij"), 0.6)) == 6  # exactly 6
    assert engine.required_solves(week("w", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z",
                                       list("abcd"), 0.5)) == 2


def test_tie_shares_rank():
    w = week("w1", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z", ["a"], 1.0)
    history = {"ann": {"a": ["2026-10-01T01:00:00+00:00"]},
               "bob": {"a": ["2026-10-01T01:00:00+00:00"]},
               "cat": {"a": ["2026-10-01T02:00:00+00:00"]}}
    rows = engine.score_week(w, TRAINEES, history, {})
    assert [r["rank"] for r in rows] == [1, 1, 3]


def test_overrides():
    w = week("w1", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z", ["a", "b"], 1.0)
    history = {"ann": {"a": ["2026-10-01T01:00:00+00:00"], "b": ["2026-10-01T02:00:00+00:00"]}}
    overrides = {
        "remove": [{"username": "ann", "slug": "b"}],
        "add": [{"username": "bob", "slug": "a", "solved_at": "2026-10-01T03:00:00Z"}],
    }
    by = {r["username"]: r for r in engine.score_week(w, TRAINEES, history, overrides)}
    assert by["ann"]["solved"] == 1      # b removed
    assert by["bob"]["solved"] == 1      # a added manually
    assert by["bob"]["time_minutes"] == 180


def _catchup_scenario():
    """Three weeks, 2 problems each, threshold 0.5 -> 1 solve needed. Catch-up = end of next week."""
    weeks = [
        week("w1", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z", ["a1", "a2"], 0.5),
        week("w2", "2026-10-08T00:00:00Z", "2026-10-15T00:00:00Z", ["b1", "b2"], 0.5),
        week("w3", "2026-10-15T00:00:00Z", "2026-10-22T00:00:00Z", ["c1", "c2"], 0.5),
    ]
    trainees = [{"username": u, "nickname": u.title(), "email": f"{u}@x.com"}
                for u in ("ann", "bob", "cat", "dan")]
    history = {
        # Ann passes everything on time.
        "ann": {"a1": ["2026-10-02T00:00:00+00:00"], "b1": ["2026-10-09T00:00:00+00:00"],
                "c1": ["2026-10-16T00:00:00+00:00"]},
        # Bob fails w1 on time, but solves a w1 problem on 10 Oct (inside the catch-up week).
        "bob": {"a1": ["2026-10-10T00:00:00+00:00"]},
        # Cat fails w1 on time and never catches up.
        "cat": {},
        # Dan passes w1 on time, then does nothing.
        "dan": {"a1": ["2026-10-02T00:00:00+00:00"]},
    }
    return weeks, trainees, history


def _evaluate(now_iso, confirmed=(), excused_w1=()):
    weeks, trainees, history = _catchup_scenario()
    if excused_w1:
        weeks[0]["excused"] = list(excused_w1)
    now = engine.parse_time(now_iso)
    scored = {w["id"]: engine.score_week(w, trainees, history, {},
                                         engine.catchup_deadline(weeks, i))
              for i, w in enumerate(weeks)}
    return engine.evaluate(weeks, scored, now, list(confirmed))


def test_late_passer_unlocks_immediately_but_on_time_passer_waits():
    # 12 Oct: w1 has ended, its catch-up window (until 15 Oct) is still open, w2 is active.
    ev = _evaluate("2026-10-12T00:00:00Z")
    assert ev["ann"]["history"][:2] == ["pass", "in_progress"]
    assert ev["ann"]["access"] == ["w1", "w2"]
    # Bob reached the threshold late (10 Oct): w2 opens for him right away, no waiting.
    assert ev["bob"]["history"][:2] == ["late_pass", "in_progress"]
    assert ev["bob"]["access"] == ["w1", "w2"] and ev["bob"]["status"] == "on_track"
    # Cat has not reached the threshold yet: still locked on w1 and marked behind.
    assert ev["cat"]["history"][0] == "behind" and ev["cat"]["status"] == "behind"
    assert ev["cat"]["access"] == ["w1"]


def test_no_early_unlock_before_week_ends():
    weeks, trainees, _ = _catchup_scenario()
    history = {"ann": {"a1": ["2026-10-02T00:00:00+00:00"]}}   # passes w1 on day 1
    now = engine.parse_time("2026-10-03T00:00:00Z")               # w1 still active
    scored = {w["id"]: engine.score_week(w, trainees, history, {},
                                         engine.catchup_deadline(weeks, i))
              for i, w in enumerate(weeks)}
    ev = engine.evaluate(weeks, scored, now, [])
    assert ev["ann"]["history"][0] == "in_progress" and ev["ann"]["access"] == ["w1"]


def test_late_pass_then_next_week_has_its_own_deadline():
    # 16 Oct: w1 catch-up deadline (15 Oct) has passed.
    ev = _evaluate("2026-10-16T00:00:00Z")
    assert ev["bob"]["history"][0] == "late_pass"
    assert "w2" in ev["bob"]["access"]
    # Bob never solved anything on w2 itself: its on-time deadline (15 Oct) has passed
    # and its catch-up window runs until 22 Oct.
    assert ev["bob"]["history"][1] == "behind" and ev["bob"]["status"] == "behind"
    # Cat never caught up on w1: two weeks without passing -> flagged, w2 stays locked.
    assert ev["cat"]["status"] == "flagged" and ev["cat"]["history"][:2] == ["failed", "locked"]
    assert ev["cat"]["access"] == ["w1"]
    # Dan passed w1, but w2 ended without a pass and its catch-up window is open.
    assert ev["dan"]["history"][:2] == ["pass", "behind"] and ev["dan"]["status"] == "behind"
    assert ev["ann"]["history"] == ["pass", "pass", "in_progress"] and ev["ann"]["status"] == "on_track"


def test_two_weeks_without_passing_flags_then_manager_confirms():
    # 23 Oct: everything for w1 and w2 is past its catch-up deadline.
    ev = _evaluate("2026-10-23T00:00:00Z")
    assert ev["dan"]["history"][:2] == ["pass", "failed"] and ev["dan"]["status"] == "flagged"
    assert ev["bob"]["history"][:2] == ["late_pass", "failed"] and ev["bob"]["status"] == "flagged"
    assert ev["ann"]["history"] == ["pass", "pass", "pass"] and ev["ann"]["status"] == "on_track"
    assert ev["ann"]["access"] == ["w1", "w2", "w3"]
    confirmed = _evaluate("2026-10-23T00:00:00Z", confirmed=["cat"])
    assert confirmed["cat"]["status"] == "eliminated" and confirmed["cat"]["access"] == []


def test_excused_week_counts_as_passed_for_access():
    ev = _evaluate("2026-10-16T00:00:00Z", excused_w1=["cat"])
    assert ev["cat"]["history"][0] == "excused"
    assert "w2" in ev["cat"]["access"]


def test_week_state_and_no_tz_rejected():
    w = week("w", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", ["a"])
    assert engine.week_state(w, datetime(2026, 9, 30, tzinfo=UTC)) == "upcoming"
    assert engine.week_state(w, datetime(2026, 10, 1, 12, tzinfo=UTC)) == "active"
    assert engine.week_state(w, datetime(2026, 10, 3, tzinfo=UTC)) == "ended"
    try:
        engine.parse_time("2026-10-01T00:00:00")
    except ValueError:
        return
    raise AssertionError("time without offset should be rejected")


def test_full_run_and_privacy():
    """Run run.py end to end (no network) and check the public file leaks nothing private."""
    src = os.path.dirname(os.path.abspath(__file__))
    tmp = tempfile.mkdtemp()
    try:
        for name in ("run.py", "engine.py", "fetch_test.py"):
            shutil.copy(os.path.join(src, name), tmp)
        os.makedirs(os.path.join(tmp, "config"))
        os.makedirs(os.path.join(tmp, "data"))
        with open(os.path.join(tmp, "config", "trainees.csv"), "w") as f:
            f.write("username,name,email\nann,Ann Secret,ann@secret.com\n"
                    "bob,Bob Secret,bob@secret.com\n")
        weeks = [week("w1", "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z",
                      ["secret-problem-one", "secret-problem-two"], 0.5),
                 week("w2", "2026-10-08T00:00:00Z", "2026-10-15T00:00:00Z",
                      ["future-secret-problem"], 0.5),
                 week("w3", "2026-10-15T00:00:00Z", "2026-10-22T00:00:00Z",
                      ["upcoming-secret-problem"], 0.5)]
        with open(os.path.join(tmp, "config", "weeks.json"), "w") as f:
            json.dump(weeks, f)
        with open(os.path.join(tmp, "data", "history.json"), "w") as f:
            json.dump({"ann": {"secret-problem-one": ["2026-10-02T00:00:00+00:00"]}}, f)

        result = subprocess.run(
            [sys.executable, "run.py", "--no-fetch", "--now", "2026-10-09T00:00:00Z"],
            cwd=tmp, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

        public = open(os.path.join(tmp, "output", "public", "standings.json")).read()
        for secret in ("secret.com", "secret-problem", "future-secret", "Secret"):
            assert secret not in public, f"public file leaks: {secret}"
        data = json.loads(public)
        assert [r["username"] for r in data["weeks"][1]["standings"]] == ["ann", "bob"]
        assert "standings" not in data["weeks"][2]          # upcoming week is not published
        assert set(data["trainees"]) == {"ann", "bob"}

        with open(os.path.join(tmp, "output", "private", "eligibility.csv")) as f:
            rows = {r["username"]: r for r in csv.DictReader(f)}
        assert rows["ann"]["groups"] == "w1;w2" and rows["ann"]["email"] == "ann@secret.com"
        assert rows["bob"]["groups"] == "w1" and rows["bob"]["status"] == "behind"
        assert rows["ann"]["name"] == "Ann Secret"
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS  {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
