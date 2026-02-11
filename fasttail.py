#!/usr/bin/env python3
"""Fetch the last N emails from Fastmail using the JMAP API."""

import argparse
import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"


def get_session(token):
    r = requests.get(JMAP_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def get_api_info(token):
    session = get_session(token)
    api_url = session["apiUrl"]
    account_id = list(session["accounts"].keys())[0]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return api_url, account_id, headers


def fetch_mailboxes(api_url, account_id, headers):
    body = {
        "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
        "methodCalls": [
            [
                "Mailbox/get",
                {"accountId": account_id, "properties": ["id", "name"]},
                "0",
            ],
        ],
    }
    r = requests.post(api_url, headers=headers, json=body)
    r.raise_for_status()
    mailbox_list = r.json()["methodResponses"][0][1]["list"]
    return {m["id"]: m["name"] for m in mailbox_list}


def fetch_emails(api_url, account_id, headers, limit=10, after=None):
    email_filter = {}
    if after:
        email_filter["after"] = after

    body = {
        "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
        "methodCalls": [
            [
                "Email/query",
                {
                    "accountId": account_id,
                    "filter": email_filter,
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": limit,
                },
                "0",
            ],
            [
                "Email/get",
                {
                    "accountId": account_id,
                    "#ids": {
                        "resultOf": "0",
                        "name": "Email/query",
                        "path": "/ids/*",
                    },
                    "properties": [
                        "id",
                        "subject",
                        "from",
                        "receivedAt",
                        "size",
                        "mailboxIds",
                    ],
                },
                "1",
            ],
        ],
    }

    r = requests.post(api_url, headers=headers, json=body)
    r.raise_for_status()
    response = r.json()

    email_get_result = response["methodResponses"][1]
    if email_get_result[0] == "error":
        raise RuntimeError(f"JMAP error: {email_get_result[1]}")

    return email_get_result[1]["list"]


def format_date(iso_date):
    dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
    local_dt = dt.astimezone()
    return local_dt.strftime("%a %b %d %H:%M:%S %Y")


def format_email(email, mailboxes, use_color=False):
    date = format_date(email["receivedAt"])
    sender = email.get("from", [{}])
    sender_addr = sender[0].get("email", "unknown") if sender else "unknown"
    subject = email.get("subject", "(no subject)")
    size = email.get("size", 0)
    mbox_ids = email.get("mailboxIds", {})
    folder = next((mailboxes.get(mid, mid) for mid in mbox_ids), "unknown")

    if use_color:
        return (
            f"From \033[1;32m{sender_addr}\033[0m  \033[33m{date}\033[0m\n"
            f" Subject: \033[1;37m{subject}\033[0m\n"
            f"  Folder: \033[36m{folder}\033[0m\t\033[2m{size}\033[0m\n"
        )
    return (
        f"From {sender_addr}  {date}\n"
        f" Subject: {subject}\n"
        f"  Folder: {folder}\t{size}\n"
    )


def run_oneshot(token, n, use_color, no_pager):
    api_url, account_id, headers = get_api_info(token)
    mailboxes = fetch_mailboxes(api_url, account_id, headers)
    emails = fetch_emails(api_url, account_id, headers, limit=n)

    buf = io.StringIO()
    for email in emails:
        buf.write(format_email(email, mailboxes, use_color))

    output = buf.getvalue()

    if no_pager or not sys.stdout.isatty():
        sys.stdout.write(output)
    else:
        pager = subprocess.Popen(["less", "-r"], stdin=subprocess.PIPE)
        pager.communicate(input=output.encode())


def run_daemon(token, logfile, interval, backfill=0, use_color=False):
    print(f"Starting daemon, polling every {interval}s, logging to {logfile}",
          file=sys.stderr)

    api_url, account_id, headers = get_api_info(token)
    mailboxes = fetch_mailboxes(api_url, account_id, headers)
    seen_ids = set()

    # Seed with current emails so we don't dump the entire inbox on first run
    seed_limit = max(50, backfill)
    emails = fetch_emails(api_url, account_id, headers, limit=seed_limit)
    for email in emails:
        seen_ids.add(email["id"])

    last_check = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(logfile, "a") as f:
        ts = datetime.now().strftime("%a %b %d %H:%M:%S %Y")
        f.write(f"# daemon started {ts}, watching for new mail\n")

        if backfill > 0:
            backfill_emails = sorted(emails[:backfill],
                                     key=lambda e: e["receivedAt"])
            for email in backfill_emails:
                f.write(format_email(email, mailboxes, use_color))

        f.flush()


        while True:
            time.sleep(interval)
            try:
                emails = fetch_emails(
                    api_url, account_id, headers, limit=50, after=last_check
                )
                new_emails = [e for e in emails if e["id"] not in seen_ids]

                if new_emails:
                    # Sort oldest first for chronological log order
                    new_emails.sort(key=lambda e: e["receivedAt"])
                    for email in new_emails:
                        seen_ids.add(email["id"])
                        f.write(format_email(email, mailboxes, use_color))
                    f.flush()

                last_check = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (401, 403):
                    print("Session expired, refreshing...", file=sys.stderr)
                    try:
                        api_url, account_id, headers = get_api_info(token)
                        mailboxes = fetch_mailboxes(api_url, account_id, headers)
                    except Exception as re:
                        print(f"Session refresh failed: {re}", file=sys.stderr)
                else:
                    print(f"Poll error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"Poll error: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Fetch recent emails from Fastmail")
    parser.add_argument(
        "-n", type=int, default=10, help="Number of emails to fetch (default: 10)"
    )
    parser.add_argument(
        "--no-pager", action="store_true", help="Disable built-in pager"
    )
    parser.add_argument(
        "--color", choices=["auto", "always", "never"], default="auto",
        help="Color output: auto (default), always, or never"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as daemon, polling for new emails and appending to logfile"
    )
    parser.add_argument(
        "--logfile", default=os.path.expanduser("~/.fastmail.log"),
        help="Log file path for daemon mode (default: ~/.fastmail.log)"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds for daemon mode (default: 60)"
    )
    parser.add_argument(
        "--backfill", type=int, default=0, metavar="N",
        help="Write last N emails to log on daemon startup (default: 0)"
    )
    args = parser.parse_args()

    token = os.environ.get("FASTMAIL_TOKEN")
    if not token:
        print("Error: FASTMAIL_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    if args.color == "always":
        use_color = True
    elif args.color == "never":
        use_color = False
    else:
        use_color = sys.stdout.isatty()

    if args.daemon:
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        run_daemon(token, args.logfile, args.interval, args.backfill, use_color)
    else:
        run_oneshot(token, args.n, use_color, args.no_pager)


if __name__ == "__main__":
    main()
