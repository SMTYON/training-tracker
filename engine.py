"""
Scoring and filtration engine. Pure logic: no network, no file access.

RANKING (Codeforces style), per week, using ON-TIME solves only:
  1. More problems solved ranks higher.
  2. Ties are broken by total time = sum over solved problems of the minutes between
     the week's start and that problem's first accepted solve. Lower is better.
  Ties share a rank.

PASSING AND CATCH-UP:
  Each sheet has an on-time deadline (week.end) and a catch-up deadline (by default the
  end of the NEXT week; override with "catchup_end" in weeks.json).
    - Solving at least `required` problems by week.end            -> "pass"
    - Reaching `required` after week.end but by the catch-up date -> "late_pass"
    - Not reaching it by the catch-up date                        -> "failed" (flagged)
  Sheet N+1 unlocks for a trainee:
    - at week N's end, if they passed on time (never earlier, even if they reached the
      threshold mid-week), or
    - IMMEDIATELY, if week N has already ended and they reach the threshold during the
      catch-up window (the next tracker run picks it up).
  While locked out of N+1, a trainee keeps working on sheet N. Sheet N+1 keeps its own
  deadlines, so an early catch-up leaves the trainee the rest of week N+1 as normal.

STATUS:
  on_track   nothing outstanding
  behind     a sheet was not passed on time and its catch-up window is still open
  flagged    a sheet was not passed even by its catch-up deadline (manager must confirm)
  eliminated flagged AND confirmed by the manager in config/eliminations.json
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def parse_time(value: str) -> datetime:
    """Parse an ISO time that MUST include an offset, e.g. 2026-10-23T19:00:00+03:00."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"Time '{value}' has no timezone offset (use e.g. +03:00 or Z).")
    return dt.astimezone(timezone.utc)


def week_state(week: dict, now: datetime) -> str:
    if now < parse_time(week["start"]):
        return "upcoming"
    if now < parse_time(week["end"]):
        return "active"
    return "ended"


def required_solves(week: dict) -> int:
    return math.ceil(week["threshold"] * len(week["problems"]) - 1e-9)


def catchup_deadline(weeks: list[dict], index: int) -> datetime:
    w = weeks[index]
    if w.get("catchup_end"):
        return parse_time(w["catchup_end"])
    if index + 1 < len(weeks):
        return parse_time(weeks[index + 1]["end"])
    return parse_time(w["end"]) + timedelta(days=7)


def effective_solves(username: str, history: dict, overrides: dict) -> dict[str, list[datetime]]:
    """History plus manual overrides, as {slug: [datetime, ...]} for one trainee."""
    result: dict[str, list[datetime]] = {}
    for slug, times in history.get(username, {}).items():
        result[slug] = [parse_time(t) for t in times]

    removed = {r["slug"] for r in overrides.get("remove", []) if r["username"] == username}
    for slug in removed:
        result.pop(slug, None)

    for add in overrides.get("add", []):
        if add["username"] == username:
            result.setdefault(add["slug"], []).append(parse_time(add["solved_at"]))
    return result


def score_week(week: dict, trainees: list[dict], history: dict, overrides: dict,
               catchup_end: datetime | None = None) -> list[dict]:
    """One scored row per trainee for one week, ranked by on-time performance."""
    start, end = parse_time(week["start"]), parse_time(week["end"])
    catchup_end = catchup_end or end
    required = required_solves(week)
    rows = []

    for t in trainees:
        solves = effective_solves(t["username"], history, overrides)
        per_problem = []
        total_minutes = 0
        solved = 0           # on time: inside [start, end]
        solved_catchup = 0   # by the catch-up deadline: inside [start, catchup_end]
        for slug in week["problems"]:
            times = solves.get(slug, [])
            in_window = [dt for dt in times if start <= dt <= end]
            if in_window:
                minutes = int((min(in_window) - start).total_seconds() // 60)
                per_problem.append(minutes)
                total_minutes += minutes
                solved += 1
            else:
                per_problem.append(None)
            if any(start <= dt <= catchup_end for dt in times):
                solved_catchup += 1

        rows.append({
            "username": t["username"],
            "nickname": t.get("nickname") or t["username"],
            "email": t.get("email", ""),
            "solved": solved,
            "solved_with_catchup": solved_catchup,
            "total": len(week["problems"]),
            "required": required,
            "on_time": solved >= required,
            "passed": solved_catchup >= required,
            "time_minutes": total_minutes,
            "per_problem": per_problem,
        })

    rows.sort(key=lambda r: (-r["solved"], r["time_minutes"], r["username"].lower()))
    rank, prev_key = 0, None
    for i, r in enumerate(rows, start=1):
        key = (r["solved"], r["time_minutes"])
        if key != prev_key:
            rank, prev_key = i, key
        r["rank"] = rank
    return rows


def is_excused(week: dict, username: str) -> bool:
    return bool(week.get("excused_all")) or username in week.get("excused", [])


def evaluate(weeks: list[dict], scored: dict[str, list[dict]], now: datetime,
             confirmed_eliminations: list[str]) -> dict[str, dict]:
    """
    Per trainee: week-by-week outcome, status, and which sheets they may open.

    history entries: upcoming, in_progress, pass, late_pass, behind, failed, excused, locked
    `access` = list of week ids the trainee should currently be able to open.
    """
    result: dict[str, dict] = {}
    usernames = []
    for rows in scored.values():
        for r in rows:
            if r["username"] not in usernames:
                usernames.append(r["username"])

    for username in usernames:
        line: list[str] = []
        access: list[str] = [weeks[0]["id"]] if weeks else []
        blocked = False   # True once an earlier sheet is unresolved: later sheets stay locked
        behind = False
        failed = False

        for i, w in enumerate(weeks):
            state = week_state(w, now)
            if blocked:
                line.append("upcoming" if state == "upcoming" else "locked")
                continue

            row = next(r for r in scored[w["id"]] if r["username"] == username)
            end = parse_time(w["end"])
            catchup = catchup_deadline(weeks, i)

            if state == "upcoming":
                line.append("upcoming")
                blocked = True
            elif state == "active":
                line.append("in_progress")
                blocked = True
            elif is_excused(w, username):
                line.append("excused")
                if i + 1 < len(weeks):
                    access.append(weeks[i + 1]["id"])
            elif row["on_time"]:                      # week ended, passed on time
                line.append("pass")
                if i + 1 < len(weeks):
                    access.append(weeks[i + 1]["id"])
            elif row["passed"]:                       # reached the threshold late: unlock NOW
                line.append("late_pass")
                if i + 1 < len(weeks):
                    access.append(weeks[i + 1]["id"])
            elif now < catchup:                       # still short, catch-up window open
                line.append("behind")
                behind = True
                blocked = True
            else:                                     # catch-up deadline passed, still short
                line.append("failed")
                failed = True
                blocked = True

        if username in confirmed_eliminations:
            status, misses = "eliminated", 2
        elif failed:
            status, misses = "flagged", 2
        elif behind:
            status, misses = "behind", 1
        else:
            status, misses = "on_track", 0

        groups = [] if status == "eliminated" else access
        result[username] = {
            "status": status,
            "consecutive_misses": misses,
            "history": line,
            "access": groups,
        }
    return result
