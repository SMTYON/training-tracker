#!/usr/bin/env python3
"""
Week 0 fetch test.

Asks LeetCode for one user's most recent ACCEPTED submissions and prints them.
Uses only the Python standard library, so there is nothing to install.

Usage:
    python fetch_test.py <leetcode_username> [limit]

NOTE: This uses LeetCode's unofficial GraphQL endpoint. It can change or be
blocked at any time, which is exactly what this test is meant to find out.
"""

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENDPOINT = "https://leetcode.com/graphql"

QUERY = """
query recentAcSubmissions($username: String!, $limit: Int!) {
  recentAcSubmissionList(username: $username, limit: $limit) {
    id
    title
    titleSlug
    timestamp
  }
}
"""


def fetch_recent_accepted(username: str, limit: int = 20) -> list[dict]:
    """The ONLY function that talks to LeetCode. If LeetCode changes, fix it here."""
    payload = json.dumps(
        {"query": QUERY, "variables": {"username": username, "limit": limit}}
    ).encode("utf-8")

    request = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Referer": "https://leetcode.com",
            "User-Agent": "Mozilla/5.0 (compatible; training-event-tracker/0.1)",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    if body.get("errors"):
        raise RuntimeError(f"LeetCode returned errors: {body['errors']}")

    submissions = (body.get("data") or {}).get("recentAcSubmissionList")
    if submissions is None:
        raise RuntimeError(
            "No submission list returned. The username may not exist, "
            "or the response format has changed."
        )

    return [
        {
            "title": s["title"],
            "slug": s["titleSlug"],
            "solved_at": datetime.fromtimestamp(int(s["timestamp"]), tz=timezone.utc),
        }
        for s in submissions
    ]


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python fetch_test.py <leetcode_username> [limit]")
        return 2

    username = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print(f"Fetching up to {limit} recent accepted submissions for '{username}'...")

    try:
        results = fetch_recent_accepted(username, limit)
    except urllib.error.HTTPError as e:
        print(f"FAILED: HTTP {e.code} {e.reason}")
        if e.code in (403, 429):
            print("LeetCode is blocking or rate-limiting this request.")
        return 1
    except Exception as e:  # noqa: BLE001 - this is a diagnostic script
        print(f"FAILED: {type(e).__name__}: {e}")
        return 1

    if not results:
        print("OK, but the list is empty (no recent accepted submissions, "
              "or the profile is private).")
        return 0

    print(f"OK, got {len(results)} submissions:\n")
    for r in results:
        print(f"  {r['solved_at']:%Y-%m-%d %H:%M} UTC  |  {r['title']}  ({r['slug']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
