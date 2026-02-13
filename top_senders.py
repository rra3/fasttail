#!/usr/bin/env python3
"""Show top email senders from Fastmail across all mailboxes."""

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"
JMAP_USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]
BATCH_SIZE = 50
MAX_RETRIES = 5
RETRY_BACKOFF = 2  # seconds, doubled each retry


def get_api_info(token):
    r = requests.get(JMAP_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    session = r.json()
    api_url = session["apiUrl"]
    account_id = list(session["accounts"].keys())[0]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return api_url, account_id, headers


def fetch_email_batch(api_url, account_id, headers, after, position,
                      properties, extra_filter=None, calculate_total=False):
    """Fetch a batch of emails starting at position."""
    filt = {"after": after}
    if extra_filter:
        filt.update(extra_filter)

    query_params = {
        "accountId": account_id,
        "filter": filt,
        "sort": [{"property": "receivedAt", "isAscending": False}],
        "position": position,
        "limit": BATCH_SIZE,
    }
    if calculate_total:
        query_params["calculateTotal"] = True

    body = {
        "using": JMAP_USING,
        "methodCalls": [
            ["Email/query", query_params, "0"],
            [
                "Email/get",
                {
                    "accountId": account_id,
                    "#ids": {
                        "resultOf": "0",
                        "name": "Email/query",
                        "path": "/ids/*",
                    },
                    "properties": properties,
                },
                "1",
            ],
        ],
    }
    r = requests.post(api_url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    resp = r.json()

    query_result = resp["methodResponses"][0][1]
    total = query_result.get("total")

    get_result = resp["methodResponses"][1]
    if get_result[0] == "error":
        raise RuntimeError(f"JMAP error: {get_result[1]}")

    emails = get_result[1]["list"]
    return emails, total


def extract_addr(email, field):
    """Extract the first email address from a header field."""
    addrs = email.get(field) or [{}]
    return addrs[0].get("email", "unknown").lower()


def collect_emails(token, api_url, account_id, headers, after):
    """Paginate through all emails since `after` and return (from, to) pairs."""
    records = []
    position = 0
    total = None
    retries = 0

    while True:
        try:
            need_total = total is None
            emails, batch_total = fetch_email_batch(
                api_url, account_id, headers, after, position,
                ["from", "to"], calculate_total=need_total,
            )
            retries = 0  # reset on success

            if need_total and batch_total is not None:
                total = batch_total

            if not emails:
                break

            for email in emails:
                records.append({
                    "from": extract_addr(email, "from"),
                    "to": extract_addr(email, "to"),
                })

            position += len(emails)
            total_str = str(total) if total else "?"
            print(
                f"\r  fetched {position}/{total_str} emails...",
                end="", file=sys.stderr,
            )

            if total and position >= total:
                break

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * (2 ** (retries - 1))
            print(
                f"\n  connection error, retrying in {wait}s ({retries}/{MAX_RETRIES})...",
                file=sys.stderr,
            )
            time.sleep(wait)
            # refresh session in case it expired
            api_url, account_id, headers = get_api_info(token)

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                retries += 1
                if retries > MAX_RETRIES:
                    raise
                print("\n  session expired, refreshing...", file=sys.stderr)
                time.sleep(1)
                api_url, account_id, headers = get_api_info(token)
            else:
                raise

    print(file=sys.stderr)
    return records


def main():
    parser = argparse.ArgumentParser(description="Top email senders from Fastmail")
    parser.add_argument(
        "-n", type=int, default=25, help="Number of top senders to show (default: 25)"
    )
    parser.add_argument(
        "--months", type=int, default=6,
        help="How many months back to look (default: 6)"
    )
    parser.add_argument(
        "--sender", type=str, default=None,
        help="Drill into a specific sender to see which of your addresses they send to"
    )
    parser.add_argument(
        "--save", type=str, default=None, metavar="FILE",
        help="Save fetched email data to a JSON file for later reuse"
    )
    parser.add_argument(
        "--load", type=str, default=None, metavar="FILE",
        help="Load previously saved email data instead of fetching from API"
    )
    args = parser.parse_args()

    if args.load:
        print(f"Loading emails from {args.load}...", file=sys.stderr)
        with open(args.load) as f:
            records = json.load(f)
        print(f"  loaded {len(records)} emails", file=sys.stderr)
    else:
        token = os.environ.get("FASTMAIL_TOKEN")
        if not token:
            print("Error: FASTMAIL_TOKEN environment variable not set", file=sys.stderr)
            sys.exit(1)

        since = datetime.now(timezone.utc) - timedelta(days=args.months * 30)
        after = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"Fetching emails since {since.strftime('%Y-%m-%d')}...", file=sys.stderr)

        api_url, account_id, headers = get_api_info(token)
        records = collect_emails(token, api_url, account_id, headers, after)

        if args.save:
            with open(args.save, "w") as f:
                json.dump(records, f)
            print(f"  saved {len(records)} emails to {args.save}", file=sys.stderr)

    if args.sender:
        filtered = [r for r in records if r["from"] == args.sender.lower()]
        counts = Counter(r["to"] for r in filtered)
        total = len(filtered)
        label = "recipient addresses"
    else:
        counts = Counter(r["from"] for r in records)
        total = len(records)
        label = "unique senders"

    top = counts.most_common(args.n)
    if not top:
        print("No emails found.", file=sys.stderr)
        return

    rank_width = len(str(args.n))
    count_width = len(str(top[0][1]))

    for i, (addr, count) in enumerate(top, 1):
        print(f"  {i:>{rank_width}}. {count:>{count_width}}  {addr}")

    print(f"\n  {len(counts)} {label}, {total} emails total", file=sys.stderr)


if __name__ == "__main__":
    main()
