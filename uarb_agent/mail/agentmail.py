"""AgentMail (agentmail.to) transport over its REST API.

Free tier: 3 inboxes, 3,000 emails/month, inline attachments capped at 6 MB per
request, so pair it with UARB_ATTACHMENT_MB=4 or so.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .base import InboundMessage, OutboundMessage, Transport

API = "https://api.agentmail.to/v0"


class AgentMailTransport(Transport):
    def __init__(self, settings):
        self.s = settings
        self.inbox_id = settings.agentmail_inbox_id
        self.address = settings.agent_address or self.inbox_id
        self.client = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {settings.agentmail_api_key}"},
            timeout=120,
        )
        self.state_path = Path(settings.data_dir) / "agentmail_state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._processed: set[str] = set()
        if self.state_path.exists():
            self._processed = set(json.loads(self.state_path.read_text()).get("processed", []))

    def fetch_unprocessed(self) -> list[InboundMessage]:
        r = self.client.get(f"/inboxes/{self.inbox_id}/messages", params={"limit": 50, "labels": "received"})
        r.raise_for_status()
        out: list[InboundMessage] = []
        for m in r.json().get("messages", []):
            mid = m.get("message_id") or m.get("id")
            if not mid or mid in self._processed:
                continue
            sender = (m.get("from") or m.get("from_") or "").lower()
            if "<" in sender:
                sender = sender.split("<", 1)[1].rstrip(">")
            if self.s.allowed_senders and sender not in self.s.allowed_senders:
                continue
            body = m.get("extracted_text") or m.get("text") or ""
            if not body and m.get("preview"):
                body = m["preview"]
            ts = m.get("timestamp") or m.get("created_at")
            try:
                received = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                received = datetime.now(timezone.utc)
            out.append(
                InboundMessage(
                    id=mid,
                    sender=sender,
                    subject=m.get("subject") or "",
                    body=body,
                    received=received,
                    thread_id=m.get("thread_id", ""),
                )
            )
        out.sort(key=lambda m: m.received)
        return out

    def mark_processed(self, message: InboundMessage) -> None:
        self._processed.add(message.id)
        self.state_path.write_text(json.dumps({"processed": sorted(self._processed)}, indent=1))

    def send(self, message: OutboundMessage) -> str:
        attachments = []
        for path in message.attachments:
            ctype, _ = mimetypes.guess_type(path.name)
            attachments.append(
                {
                    "filename": path.name,
                    "content_type": ctype or "application/octet-stream",
                    "content": base64.b64encode(path.read_bytes()).decode(),
                    "content_disposition": "attachment",
                }
            )
        payload = {"to": message.to, "subject": message.subject, "text": message.body, "attachments": attachments}
        if message.in_reply_to:
            url = f"/inboxes/{self.inbox_id}/messages/{message.in_reply_to}/reply"
            payload = {"text": message.body, "attachments": attachments}
        else:
            url = f"/inboxes/{self.inbox_id}/messages/send"
        r = self.client.post(url, json=payload)
        r.raise_for_status()
        return r.json().get("message_id", "sent")
