#!/usr/bin/env python3
"""Find and execute unsubscribe for a given email sender via Fastmail JMAP."""

import argparse
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"
JMAP_USING = ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"]

# Words that suggest an unsubscribe link
UNSUB_PATTERNS = re.compile(
    r"unsubscribe|opt[\s_-]?out|email[\s_-]?preferences|manage[\s_-]?subscriptions?"
    r"|remove[\s_-]?me|stop[\s_-]?receiving",
    re.IGNORECASE,
)

# Patterns that indicate success on a response page
SUCCESS_PATTERNS = re.compile(
    r"successfully\s+unsubscribed|you.ve been (removed|unsubscribed)"
    r"|unsubscribe[d]?\s+(successful|confirmed|complete)"
    r"|removed from.{0,30}(list|mailing)|no longer receive"
    r"|subscription.{0,20}(cancelled|canceled|removed)"
    r"|you.re unsubscribed|opt.out.{0,20}(confirmed|complete|successful)",
    re.IGNORECASE,
)


def get_api_info(token):
    r = requests.get(JMAP_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    session = r.json()
    api_url = session["apiUrl"]
    account_id = list(session["accounts"].keys())[0]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return api_url, account_id, headers


def fetch_sample_email(api_url, account_id, headers, sender, recipient=None):
    """Fetch a recent email from sender with headers and HTML body."""
    email_filter = {"from": sender}
    if recipient:
        email_filter = {"operator": "AND", "conditions": [
            {"from": sender},
            {"to": recipient},
        ]}
    body = {
        "using": JMAP_USING,
        "methodCalls": [
            [
                "Email/query",
                {
                    "accountId": account_id,
                    "filter": email_filter,
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": 1,
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
                        "subject",
                        "from",
                        "receivedAt",
                        "header:list-unsubscribe",
                        "header:list-unsubscribe-post",
                        "htmlBody",
                        "bodyValues",
                    ],
                    "fetchHTMLBodyValues": True,
                    "maxBodyValueBytes": 256000,
                },
                "1",
            ],
        ],
    }
    r = requests.post(api_url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    resp = r.json()

    get_result = resp["methodResponses"][1]
    if get_result[0] == "error":
        raise RuntimeError(f"JMAP error: {get_result[1]}")

    emails = get_result[1]["list"]
    if not emails:
        return None
    return emails[0]


def parse_list_unsubscribe_header(header_value):
    """Parse List-Unsubscribe header, return (http_urls, mailto_addrs)."""
    if not header_value:
        return [], []
    http_urls = []
    mailto_addrs = []
    for match in re.finditer(r"<([^>]+)>", header_value):
        uri = match.group(1).strip()
        if uri.startswith("http://") or uri.startswith("https://"):
            http_urls.append(uri)
        elif uri.startswith("mailto:"):
            mailto_addrs.append(uri)
    return http_urls, mailto_addrs


class UnsubLinkFinder(HTMLParser):
    """Extract unsubscribe-related links from HTML."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._current_href = None
        self._current_text = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._in_a = True
            self._current_text = []
            attr_dict = dict(attrs)
            self._current_href = attr_dict.get("href")

    def handle_data(self, data):
        if self._in_a:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_a:
            self._in_a = False
            text = " ".join(self._current_text).strip()
            href = self._current_href
            if href and (href.startswith("http://") or href.startswith("https://")):
                if UNSUB_PATTERNS.search(text) or UNSUB_PATTERNS.search(href):
                    self.links.append((href, text))
            self._current_href = None
            self._current_text = []


class FormFinder(HTMLParser):
    """Find forms on an unsubscribe confirmation page."""

    def __init__(self):
        super().__init__()
        self.forms = []
        self._current_form = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == "form":
            self._current_form = {
                "action": attr_dict.get("action", ""),
                "method": attr_dict.get("method", "GET").upper(),
                "inputs": {},
            }
            self._current_text = []
        elif tag == "input" and self._current_form is not None:
            name = attr_dict.get("name")
            value = attr_dict.get("value", "")
            if name:
                self._current_form["inputs"][name] = value

    def handle_data(self, data):
        if self._current_form is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "form" and self._current_form is not None:
            self._current_form["text"] = " ".join(self._current_text).strip()
            self.forms.append(self._current_form)
            self._current_form = None
            self._current_text = []


def find_unsub_links_in_html(email):
    """Extract unsubscribe links from email HTML body."""
    body_values = email.get("bodyValues", {})
    html_parts = email.get("htmlBody", [])
    links = []
    for part in html_parts:
        part_id = part.get("partId")
        if part_id and part_id in body_values:
            html = body_values[part_id].get("value", "")
            finder = UnsubLinkFinder()
            finder.feed(html)
            links.extend(finder.links)
    return links


def attempt_one_click_unsubscribe(url, post_value="List-Unsubscribe=One-Click"):
    """RFC 8058 one-click unsubscribe via POST."""
    print(f"  Attempting RFC 8058 one-click POST to:\n    {url}")
    try:
        r = requests.post(
            url,
            data=post_value,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            allow_redirects=True,
        )
        print(f"  Response: {r.status_code}")
        if r.status_code < 400:
            if SUCCESS_PATTERNS.search(r.text):
                return "success", "Server confirmed unsubscribe"
            # 2xx with no explicit confirmation is still likely success for one-click
            return "likely_success", f"Server returned {r.status_code} (one-click POST accepted)"
        return "error", f"Server returned {r.status_code}"
    except requests.RequestException as e:
        return "error", str(e)


def attempt_get_unsubscribe(url):
    """Try to unsubscribe by visiting a URL."""
    print(f"  Fetching unsubscribe URL:\n    {url}")
    try:
        r = requests.get(url, timeout=30, allow_redirects=True)
        print(f"  Response: {r.status_code} ({len(r.text)} bytes)")

        if r.status_code >= 400:
            return "error", f"Server returned {r.status_code}"

        # Check if just visiting the page was enough
        if SUCCESS_PATTERNS.search(r.text):
            return "success", "Page confirms unsubscribe"

        # Look for a form to submit (confirmation page)
        finder = FormFinder()
        finder.feed(r.text)

        unsub_forms = []
        for form in finder.forms:
            form_text = form["text"].lower() + " ".join(form["inputs"].values()).lower()
            if UNSUB_PATTERNS.search(form_text) or "confirm" in form_text:
                unsub_forms.append(form)

        # If only one form on the page, it's probably the confirmation
        if not unsub_forms and len(finder.forms) == 1:
            unsub_forms = finder.forms

        if unsub_forms:
            form = unsub_forms[0]
            action = form["action"]
            if not action or action == "#":
                action = url
            else:
                action = urljoin(r.url, action)

            method = form["method"]
            data = form["inputs"]

            print(f"  Found confirmation form ({method} {action})")
            print(f"    Fields: {data}")

            if method == "POST":
                r2 = requests.post(action, data=data, timeout=30, allow_redirects=True)
            else:
                r2 = requests.get(action, params=data, timeout=30, allow_redirects=True)

            print(f"  Form response: {r2.status_code}")
            if r2.status_code < 400:
                if SUCCESS_PATTERNS.search(r2.text):
                    return "success", "Confirmed unsubscribe via form submission"
                return "likely_success", f"Form submitted, server returned {r2.status_code}"

            return "error", f"Form submission returned {r2.status_code}"

        # No form found, page didn't confirm — probably needs JS or manual action
        return "manual", "Page loaded but no confirmation detected; may need a browser"

    except requests.RequestException as e:
        return "error", str(e)


def run(token, sender, dry_run=False, recipient=None):
    api_url, account_id, headers = get_api_info(token)

    if recipient:
        print(f"Looking for email from: {sender} to: {recipient}")
    else:
        print(f"Looking for email from: {sender}")
    email = fetch_sample_email(api_url, account_id, headers, sender, recipient=recipient)
    if not email:
        print(f"No emails found from {sender}", file=sys.stderr)
        return False

    subject = email.get("subject", "(no subject)")
    date = email.get("receivedAt", "?")
    print(f"  Found: \"{subject}\" ({date})")

    # Strategy 1: List-Unsubscribe header with one-click POST
    list_unsub = email.get("header:list-unsubscribe")
    list_unsub_post = email.get("header:list-unsubscribe-post")
    http_urls, mailto_addrs = parse_list_unsubscribe_header(list_unsub)

    if http_urls and list_unsub_post and "List-Unsubscribe=One-Click" in list_unsub_post:
        print("\n  Method: RFC 8058 one-click unsubscribe (best)")
        if dry_run:
            print(f"  [DRY RUN] Would POST to: {http_urls[0]}")
            return True
        status, msg = attempt_one_click_unsubscribe(http_urls[0])
        print(f"\n  Result: [{status}] {msg}")
        return status in ("success", "likely_success")

    # Strategy 2: List-Unsubscribe header URL (GET)
    if http_urls:
        print(f"\n  Method: List-Unsubscribe header URL")
        if dry_run:
            print(f"  [DRY RUN] Would visit: {http_urls[0]}")
            return True
        status, msg = attempt_get_unsubscribe(http_urls[0])
        print(f"\n  Result: [{status}] {msg}")
        if status == "manual":
            print(f"\n  Open manually: {http_urls[0]}")
        return status in ("success", "likely_success")

    # Strategy 3: Find unsubscribe link in HTML body
    body_links = find_unsub_links_in_html(email)
    if body_links:
        # Prefer links with "unsubscribe" in the text
        best = body_links[0]
        for href, text in body_links:
            if "unsubscribe" in text.lower():
                best = (href, text)
                break

        href, text = best
        print(f"\n  Method: HTML body link (\"{text}\")")
        if dry_run:
            print(f"  [DRY RUN] Would visit: {href}")
            return True
        status, msg = attempt_get_unsubscribe(href)
        print(f"\n  Result: [{status}] {msg}")
        if status == "manual":
            print(f"\n  Open manually: {href}")
        return status in ("success", "likely_success")

    # No unsubscribe mechanism found
    if mailto_addrs:
        print(f"\n  No HTTP unsubscribe found. Mailto only: {mailto_addrs[0]}")
    else:
        print("\n  No unsubscribe link found in headers or body.")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Unsubscribe from a sender via Fastmail"
    )
    parser.add_argument("sender", help="Email address to unsubscribe from")
    parser.add_argument(
        "--to", dest="recipient", default=None,
        help="Only match emails sent to this recipient address"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Find the unsubscribe link but don't click it"
    )
    args = parser.parse_args()

    token = os.environ.get("FASTMAIL_TOKEN")
    if not token:
        print("Error: FASTMAIL_TOKEN environment variable not set", file=sys.stderr)
        sys.exit(1)

    ok = run(token, args.sender, dry_run=args.dry_run, recipient=args.recipient)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
