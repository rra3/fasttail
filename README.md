# fastmail-tools

A collection of command-line tools for Fastmail, built on the JMAP API.

## Setup

Requires Python 3 and the `requests` library:

```bash
pip install requests
```

Create a Fastmail API token at **Settings > Privacy & Security > API tokens** with at least these scopes:

- `urn:ietf:params:jmap:core`
- `urn:ietf:params:jmap:mail`

Export it:

```bash
export FASTMAIL_TOKEN=your_token_here
```

## Tools

### fasttail — fetch and tail recent emails

Procmail-style log output for your Fastmail inbox.

```bash
python3 fasttail.py          # last 10 emails
python3 fasttail.py -n 20    # last 20 emails
```

Output:

```
From sender@example.com  Wed Feb 11 14:07:58 2026
 Subject: The innovation is real
  Folder: Inbox	136985
```

Options:

- `--color auto|always|never` — color output (default: `auto`)
- `--no-pager` — disable built-in `less` pager

#### Daemon mode

Poll for new messages and append to a log file:

```bash
python3 fasttail.py --daemon &
tail -f ~/.fastmail.log
```

- `--logfile PATH` — log file path (default: `~/.fastmail.log`)
- `--interval SECONDS` — polling interval (default: `60`)
- `--backfill N` — write last N emails to log on startup (default: `0`)

### top_senders — rank senders by volume

Show your top email senders across all mailboxes.

```bash
python3 top_senders.py              # top 25 senders, last 6 months
python3 top_senders.py -n 50        # top 50
python3 top_senders.py --months 12  # last year
```

Output:

```
   1. 904  hello@pacas.us
   2. 783  harryanddavid@harryanddavid-email.com
   3. 772  info@jomashop.com
  ...
```

#### Drill into a sender

See which of your addresses a specific sender is writing to (useful with multiple aliases):

```bash
python3 top_senders.py --sender hello@pacas.us
```

#### Save and reload data

Fetch once and save to avoid repeated API calls, then reuse for drill-downs:

```bash
python3 top_senders.py --save emails.json              # fetch and save
python3 top_senders.py --load emails.json               # top senders from saved data
python3 top_senders.py --load emails.json --sender hello@pacas.us  # drill down offline
```

### unsubscribe — unsubscribe from a sender

Find and execute the unsubscribe mechanism for a given sender.

```bash
python3 unsubscribe.py hello@pacas.us                        # unsubscribe
python3 unsubscribe.py --dry-run hello@pacas.us               # just show what it would do
python3 unsubscribe.py hello@pacas.us --to myalias@fastmail.com  # match by recipient
```

Tries three strategies in priority order:

1. **RFC 8058 one-click POST** — `List-Unsubscribe-Post` header (most reliable)
2. **List-Unsubscribe header URL** — visit the URL, submit any confirmation form
3. **HTML body link** — parse the email for unsubscribe links

Some senders require JavaScript or manual confirmation; the tool will print the URL to open in a browser when it can't complete the process automatically.

### trash_by_sender — move emails from a sender to Trash

Bulk-move all emails from a given sender to your Trash folder.

```bash
python3 trash_by_sender.py someone@example.com             # move all to trash
python3 trash_by_sender.py someone@example.com --dry-run    # preview without moving
python3 trash_by_sender.py someone@example.com --limit 10   # move at most 10
```

Options:

- `--dry-run` — list matched emails without moving them
- `--limit N` — cap the number of emails to move
