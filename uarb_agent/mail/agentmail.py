"""AgentMail (agentmail.to) transport over its REST API.

Free tier: 3 inboxes, 3,000 emails/month, inline attachments capped at 6 MB per
request, so pair it with UARB_ATTACHMENT_MB=4 or so.

"Processed" is recorded on the server by adding a label to the message, so
several workers can share the inbox.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from datetime import datetime, timezone

import httpx

from .base import InboundMessage, OutboundMessage, Transport
from .guards import looks_automated, old_enough, too_old

API = "https://api.agentmail.to/v0"
DONE_LABEL = "uarb-agent-done"
log = logging.getLogger(__name__)


class AgentMailTransport(Transport):
    def __init__(self, settings, min_age_s: int = 0):
        self.s = settings
        self.inbox_id = settings.agentmail_inbox_id
        self.address = (settings.agent_address or self.inbox_id).lower()
        self.min_age_s = min_age_s
        self.client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {settings.agentmail_api_key}"}, timeout=120)

    def fetch_unprocessed(self) -> list[InboundMessage]:
        r = self.client.get(f"/inboxes/{self.inbox_id}/messages", params={"limit": 50})
        r.raise_for_status()
        out: list[InboundMessage] = []
        for m in r.json().get("messages", []):
            mid = m.get("message_id") or m.get("id")
            labels = [str(x).lower() for x in (m.get("labels") or [])]
            if not mid or DONE_LABEL in labels or "sent" in labels:
                continue
            sender = (m.get("from") or m.get("from_") or "").lower()
            if "<" in sender:
                sender = sender.split("<", 1)[1].rstrip(">")
            if sender == self.address:
                self.mark_processed_id(mid)
                continue
            headers = {k.lower(): str(v) for k, v in (m.get("headers") or {}).items()}
            reason = looks_automated(sender, headers, m.get("subject") or "")
            if reason:
                log.info("ignoring %s from %s: %s", mid, sender, reason)
                self.mark_processed_id(mid)
                continue
            if self.s.allowed_senders and sender not in self.s.allowed_senders:
                self.mark_processed_id(mid)
                continue
            ts = m.get("timestamp") or m.get("created_at")
            try:
                received = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                received = datetime.now(timezone.utc)
            if too_old(received):
                self.mark_processed_id(mid)
                continue
            if not old_enough(received, self.min_age_s):
                continue
            body = m.get("extracted_text") or m.get("text") or m.get("preview") or ""
            out.append(InboundMessage(id=mid, sender=sender, subject=m.get("subject") or "", body=body, received=received, thread_id=m.get("thread_id", "")))
        out.sort(key=lambda m: m.received)
        return out

    def mark_processed_id(self, mid: str) -> None:
        r = self.client.patch(f"/inboxes/{self.inbox_id}/messages/{mid}", json={"add_labels": [DONE_LABEL]})
        if r.status_code >= 400:
            log.warning("could not label %s as done: %s %s", mid, r.status_code, r.text[:200])

    def mark_processed(self, message: InboundMessage) -> None:
        self.mark_processed_id(message.id)

    def send(self, message: OutboundMessage) -> str:
        attachments = []
        for path in message.attachments:
            ctype, _ = mimetypes.guess_type(path.name)
            attachments.append({"filename": path.name, "content_type": ctype or "application/octet-stream", "content": base64.b64encode(path.read_bytes()).decode(), "content_disposition": "attachment"})
        if message.in_reply_to:
            url = f"/inboxes/{self.inbox_id}/messages/{message.in_reply_to}/reply"
            payload = {"text": message.body, "attachments": attachments}
        else:
            url = f"/inboxes/{self.inbox_id}/messages/send"
            payload = {"to": message.to, "subject": message.subject, "text": message.body, "attachments": attachments}
        r = self.client.post(url, json=payload)
        r.raise_for_status()
        return r.json().get("message_id", "sent")
