#!/usr/bin/env python3
"""
Scale test.

Sends many requests to LeetCode (cycling through usernames.txt) with a delay
between each one, then reports how many worked, how many were blocked or
rate-limited, and how long it all took.

Usage:
    python scale_test.py <total_requests> <delay_seconds>

Example:
    python scale_test.py 100 1.0
"""

import sys
import time
import urllib.error
from collections import Counter

from fetch_test import fetch_recent_accepted


def load_usernames(path: str = "usernames.txt") -> list[str]:
    with open(path, encoding="utf-8") as f:
        names = [line.strip() for line in f if line.strip()]
    if not names:
        raise SystemExit("usernames.txt is empty")
    return names


def main() -> int:
    total = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

    usernames = load_usernames()
    print(f"Users in file: {len(usernames)}")
    print(f"Total requests: {total}, delay between requests: {delay}s")
    print(f"Expected minimum duration: about {total * delay / 60:.1f} minutes\n")

    outcomes: Counter = Counter()
    latencies: list[float] = []
    first_failure_at = None
    consecutive_blocks = 0
    started = time.time()

    for i in range(total):
        username = usernames[i % len(usernames)]
        t0 = time.time()
        try:
            results = fetch_recent_accepted(username, 20)
            outcomes["ok" if results else "ok_but_empty"] += 1
            consecutive_blocks = 0
        except urllib.error.HTTPError as e:
            outcomes[f"http_{e.code}"] += 1
            if first_failure_at is None:
                first_failure_at = i + 1
            if e.code in (403, 429):
                consecutive_blocks += 1
        except Exception as e:  # noqa: BLE001 - diagnostic script
            outcomes[f"error_{type(e).__name__}"] += 1
            if first_failure_at is None:
                first_failure_at = i + 1
        latencies.append(time.time() - t0)

        if (i + 1) % 25 == 0:
            print(f"  ...{i + 1}/{total} done, so far: {dict(outcomes)}")

        # Stop early if LeetCode is clearly blocking us, so we do not keep hammering it.
        if consecutive_blocks >= 5:
            print("\nStopping early: 5 blocked requests in a row.")
            break

        time.sleep(delay)

    elapsed = time.time() - started
    sent = sum(outcomes.values())

    print("\n===== SUMMARY =====")
    print(f"Requests sent:      {sent}")
    for name, count in sorted(outcomes.items()):
        print(f"  {name:<22} {count}")
    print(f"Total time:         {elapsed / 60:.1f} minutes ({elapsed:.0f}s)")
    if latencies:
        print(f"Avg response time:  {sum(latencies) / len(latencies):.2f}s")
        print(f"Slowest response:   {max(latencies):.2f}s")
    if first_failure_at:
        print(f"First failure at request #{first_failure_at}")
    else:
        print("No failures.")

    good = outcomes["ok"] + outcomes["ok_but_empty"]
    return 0 if sent and good == sent else 1


if __name__ == "__main__":
    sys.exit(main())
