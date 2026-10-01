"""One-command live stampede and reconciliation check (Python standard library)."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import urllib.error
import urllib.request

import app


def request(base, method, path, payload=None, token=None, timeout=90):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            body = json.load(error)
        except ValueError:
            body = {"error": "invalid-response"}
        return error.code, body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", help="e.g. http://localhost:8000")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=100)
    args = parser.parse_args()
    if args.requests < 10 or args.concurrency < 1:
        parser.error("requests must be >=10 and concurrency >=1")
    base = args.base_url.rstrip("/")
    admin = os.environ.get("ADMIN_TOKEN", "development-admin-token")
    status, show = request(base, "POST", "/shows",
                           {"name": "burst", "seats": [f"A{i}" for i in range(1, 111)],
                            "price_paise": 25000, "per_user_limit": 4}, admin)
    if status != 201:
        sys.exit(f"show creation failed: HTTP {status}: {show}")
    show_id = show["id"]
    path = f"/shows/{show_id}/reserve"
    replay_token = app.issue_token("replay-user")
    status, seeded = request(base, "POST", path,
                             {"seats": ["A100"], "idempotency_key": "replay-seed"}, replay_token)
    if status != 201:
        sys.exit(f"replay seed failed: HTTP {status}: {seeded}")

    def attempt(i):
        kind = i % 20
        if kind in (0, 1):
            payload = {"seats": ["A100" if kind == 0 else "A99"], "idempotency_key": "replay-seed"}
            token = replay_token
        elif kind < 14:
            payload = {"seats": ["A1"], "idempotency_key": f"hot-{i}"}
            token = app.issue_token(f"buyer-{i}")
        elif kind < 18:
            payload = {"seats": [f"A{2 + (i % 98)}"], "idempotency_key": f"other-{i}"}
            token = app.issue_token(f"buyer-{i}")
        else:
            payload = {"seats": [f"A{101 + ((i // 20) % 10)}"], "idempotency_key": f"limit-{i}"}
            token = app.issue_token("limited-user")
        try:
            code, body = request(base, "POST", path, payload, token)
            label = "replay" if code == 200 else "confirmed" if code == 201 else "upstream-error"
            return kind, code, body.get("error", label)
        except Exception as error:
            return kind, 0, type(error).__name__

    tally = Counter()
    hot_winners = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(attempt, i) for i in range(args.requests)]
        for future in as_completed(futures):
            kind, code, reason = future.result()
            tally[(code, reason)] += 1
            if 2 <= kind < 14 and code == 201:
                hot_winners += 1
    status, final = request(base, "GET", f"/shows/{show_id}")
    if status != 200:
        sys.exit(f"show state failed: HTTP {status}: {final}")
    counts = final["counts"]
    reconciled = counts["available"] + counts["held"] + counts["confirmed"] == counts["total_seats"]
    confirmed_by_state = sum(state == "confirmed" for state in final["seats"].values())
    print(json.dumps({"show_id": show_id, "requests": args.requests,
                      "outcomes": {f"{code} {reason}": count for (code,reason),count in sorted(tally.items())},
                      "hot_seat_winners": hot_winners, "counts": counts,
                      "reconciled": reconciled and confirmed_by_state == counts["confirmed"],
                      "five_xx": sum(count for (code,_),count in tally.items() if code >= 500 or code == 0)}, indent=2))
    if not reconciled or confirmed_by_state != counts["confirmed"] or hot_winners != 1 or any(
        code >= 500 or code == 0 for code,_ in tally
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
