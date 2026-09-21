"""Point the AgentMail inbox at the GitHub Actions workflow.

AgentMail can attach custom headers to its webhook calls, so it can call
GitHub's repository_dispatch endpoint directly: a new email becomes a workflow
run within a minute, with no server in between.

    AGENTMAIL_API_KEY=am_... GITHUB_TOKEN=github_pat_... python deploy/agentmail_webhook.py NathanL15/uarb-agent
    python deploy/agentmail_webhook.py --list
    python deploy/agentmail_webhook.py --delete <webhook_id>

The GitHub token needs only "Contents: read and write" on this one repository
(fine-grained PAT), which is what repository_dispatch requires.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

API = "https://api.agentmail.to/v0"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", help="owner/name of the GitHub repository")
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
    if not args.repo:
        ap.error("repo is required unless --list or --delete is given")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    body = {
        "url": f"https://api.github.com/repos/{args.repo}/dispatches",
        "event_types": ["message.received"],
        "headers": {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    }
    if args.inbox:
        body["inbox_ids"] = [args.inbox]
    r = client.post("/webhooks", json=body)
    if r.status_code >= 400:
        print(r.status_code, r.text, file=sys.stderr)
        return 1
    data = r.json()
    print("webhook created:", data.get("webhook_id") or data.get("id"))
    print("GitHub receives AgentMail's payload; its top-level event_type is 'message.received', which the workflow listens for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
