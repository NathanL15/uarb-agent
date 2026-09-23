"""Point the AgentMail inbox at the relay in deploy/relay.

GitHub's repository_dispatch endpoint rejects AgentMail's webhook body (it has
keys GitHub does not allow), so the webhook goes to the Cloudflare Worker,
which forwards a clean dispatch. The shared RELAY_SECRET travels as a header.

    python deploy/agentmail_webhook.py https://uarb-agent-relay.<subdomain>.workers.dev
    python deploy/agentmail_webhook.py --list
    python deploy/agentmail_webhook.py --delete <webhook_id>

Reads AGENTMAIL_API_KEY, AGENTMAIL_INBOX_ID and RELAY_SECRET from .env.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

API = "https://api.agentmail.to/v0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="the relay worker's URL")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--delete", metavar="WEBHOOK_ID")
    ap.add_argument("--inbox", default=os.environ.get("AGENTMAIL_INBOX_ID", ""), help="limit to one inbox (default: all inboxes on the account)")
    args = ap.parse_args()

    key = os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        print("AGENTMAIL_API_KEY is not set", file=sys.stderr)
        return 2
    client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {key}"}, timeout=30)

    if args.list:
        r = client.get("/webhooks")
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2))
        return 0
    if args.delete:
        r = client.delete(f"/webhooks/{args.delete}")
        r.raise_for_status()
        print("deleted", args.delete)
        return 0
    if not args.url:
        ap.error("the relay URL is required unless --list or --delete is given")
    secret = os.environ.get("RELAY_SECRET")
    if not secret:
        print("RELAY_SECRET is not set", file=sys.stderr)
        return 2

    body = {
        "url": args.url,
        "event_types": ["message.received"],
        "headers": {"X-Relay-Secret": secret},
    }
    if args.inbox:
        body["inbox_ids"] = [args.inbox]
    r = client.post("/webhooks", json=body)
    if r.status_code >= 400:
        print(r.status_code, r.text, file=sys.stderr)
        return 1
    data = r.json()
    print("webhook created:", data.get("webhook_id") or data.get("id"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
