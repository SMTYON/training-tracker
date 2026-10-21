#!/usr/bin/env python3
"""
Tracker runner.

    python run.py                 normal run: fetch (if a week is active) then compute
    python run.py --no-fetch      only recompute from saved history
    python run.py --force-fetch   fetch even when no week is active
    python run.py --now 2026-10-30T12:00:00+03:00   pretend it is this time (for testing)

Reads:   config/trainees.csv, config/weeks.json, config/overrides.json,
         config/eliminations.json, data/history.json
Writes:  data/history.json                (every accepted solve ever seen)
         output/public/standings.json     (safe to publish: LeetCode usernames, no emails or problem names)
         output/private/eligibility.csv   (emails: NEVER publish)
         output/private/fetch_report.json
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone

import engine
from fetch_test import fetch_recent_accepted

FETCH_DELAY_SECONDS = 1.0
FETCH_GRACE_AFTER_END = timedelta(hours=6)  # keep fetching a little after the catch-up deadline
MAX_FAILURE_RATE = 0.2  # more than this fraction of failed fetches => the run fails (alert)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_trainees(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = [{k.strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    seen = set()
    for r in rows:
        if not r.get("username"):
            raise SystemExit(f"trainees.csv: every row needs a username: {r}")
        # The "name" column (or old "nickname") is optional and stays private.
        r["nickname"] = r.get("name") or r.get("nickname") or r["username"]
        if r["username"].lower() in seen:
            raise SystemExit(f"trainees.csv: duplicate username {r['username']}")
        seen.add(r["username"].lower())
    return rows


def validate_weeks(weeks: list[dict]) -> None:
    ids = set()
    for w in weeks:
        for key in ("id", "name", "start", "end", "threshold", "problems"):
            if key not in w:
                raise SystemExit(f"weeks.json: week is missing '{key}': {w}")
        if w["id"] in ids:
            raise SystemExit(f"weeks.json: duplicate week id {w['id']}")
        ids.add(w["id"])
        if engine.parse_time(w["end"]) <= engine.parse_time(w["start"]):
            raise SystemExit(f"weeks.json: {w['id']} ends before it starts")
        if w.get("catchup_end") and engine.parse_time(w["catchup_end"]) < engine.parse_time(w["end"]):
            raise SystemExit(f"weeks.json: {w['id']} catchup_end is before end")
        if not 0 < w["threshold"] <= 1:
            raise SystemExit(f"weeks.json: {w['id']} threshold must be between 0 and 1")


def should_fetch(weeks: list[dict], now: datetime) -> bool:
    """Fetch from a week's start until its catch-up deadline (plus a short grace period)."""
    for i, w in enumerate(weeks):
        start = engine.parse_time(w["start"])
        stop = engine.catchup_deadline(weeks, i) + FETCH_GRACE_AFTER_END
        if start <= now <= stop:
            return True
    return False


def fetch_all(trainees: list[dict], history: dict) -> dict:
    """Fetch every trainee politely and merge new solves into history."""
    report = {"ok": 0, "failed": 0, "failures": [], "new_solves": 0}
    for t in trainees:
        username = t["username"]
        try:
            subs = fetch_recent_accepted(username, 20)
            report["ok"] += 1
            user_hist = history.setdefault(username, {})
            for s in subs:
                stamp = s["solved_at"].isoformat()
                times = user_hist.setdefault(s["slug"], [])
                if stamp not in times:
                    times.append(stamp)
                    report["new_solves"] += 1
        except urllib.error.HTTPError as e:
            report["failed"] += 1
            report["failures"].append({"username": username, "error": f"HTTP {e.code}"})
        except Exception as e:  # noqa: BLE001
            report["failed"] += 1
            report["failures"].append({"username": username, "error": f"{type(e).__name__}: {e}"})
        time.sleep(FETCH_DELAY_SECONDS)
    return report


def build_public(weeks, scored, evaluation, now) -> dict:
    """Everything here is safe to publish: LeetCode usernames only. No emails, no names, no problem slugs."""
    out_weeks = []
    for i, w in enumerate(weeks):
        state = engine.week_state(w, now)
        entry = {
            "id": w["id"], "name": w["name"], "state": state,
            "start": w["start"], "end": w["end"],
            "catchup_end": engine.catchup_deadline(weeks, i).isoformat(),
            "threshold": w["threshold"], "required": engine.required_solves(w),
            "problem_count": len(w["problems"]),
        }
        if state != "upcoming":
            entry["standings"] = [{
                "rank": r["rank"], "username": r["username"], "solved": r["solved"],
                "time_minutes": r["time_minutes"], "on_time": r["on_time"],
                "passed": r["passed"], "solved_with_catchup": r["solved_with_catchup"],
                "per_problem": r["per_problem"],
            } for r in scored[w["id"]]]
        out_weeks.append(entry)

    trainees = {u: {"status": e["status"], "history": e["history"]}
                for u, e in evaluation.items()}
    return {"generated_at": now.isoformat(), "weeks": out_weeks, "trainees": trainees}


def write_eligibility(path: str, trainees: list[dict], evaluation: dict, weeks: list[dict]) -> None:
    """
    PRIVATE. One row per trainee: which weekly Google Groups they should be in right now.
    `groups` is a semicolon-separated list of week ids. Remove people from any group not listed.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "username", "email", "status", "latest_week", "groups", "week_history"])
        for t in trainees:
            e = evaluation[t["username"]]
            latest = e["access"][-1] if e["access"] else ""
            w.writerow([t["nickname"], t["username"], t.get("email", ""), e["status"],
                        latest, ";".join(e["access"]), ";".join(e["history"])])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--force-fetch", action="store_true")
    ap.add_argument("--now", help="ISO time with offset, for testing")
    ap.add_argument("--base", default=".", help="project folder (default: current folder)")
    args = ap.parse_args()

    base = args.base
    p = lambda *parts: os.path.join(base, *parts)  # noqa: E731

    now = engine.parse_time(args.now) if args.now else datetime.now(timezone.utc)

    trainees = load_trainees(p("config", "trainees.csv"))
    weeks = load_json(p("config", "weeks.json"), [])
    validate_weeks(weeks)
    overrides = load_json(p("config", "overrides.json"), {"add": [], "remove": []})
    confirmed = load_json(p("config", "eliminations.json"), {"confirmed": []})["confirmed"]
    history = load_json(p("data", "history.json"), {})

    exit_code = 0
    report = {"fetched": False}
    if not args.no_fetch and (args.force_fetch or should_fetch(weeks, now)):
        print(f"Fetching {len(trainees)} trainees...")
        report = {"fetched": True, **fetch_all(trainees, history)}
        save_json(p("data", "history.json"), history)
        total = report["ok"] + report["failed"]
        print(f"Fetch done: {report['ok']} ok, {report['failed']} failed, "
              f"{report['new_solves']} new solves.")
        if total and report["failed"] / total > MAX_FAILURE_RATE:
            print("ALERT: too many failed fetches. LeetCode may have changed or is blocking us.")
            exit_code = 1
    else:
        print("No fetch this run (no active week, or --no-fetch).")

    scored = {w["id"]: engine.score_week(w, trainees, history, overrides,
                                         engine.catchup_deadline(weeks, i))
              for i, w in enumerate(weeks)}
    evaluation = engine.evaluate(weeks, scored, now, confirmed)

    save_json(p("output", "public", "standings.json"),
              build_public(weeks, scored, evaluation, now))
    write_eligibility(p("output", "private", "eligibility.csv"), trainees, evaluation, weeks)
    save_json(p("output", "private", "fetch_report.json"), report)
    print("Wrote output/public/standings.json and output/private/eligibility.csv")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
