"""AgentMail transport against a fake API: list, fetch body, label, reply."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx

from uarb_agent.mail.agentmail import DONE_LABEL, AgentMailTransport
from uarb_agent.mail.base import OutboundMessage

INBOX = "agent@agentmail.to"
NOW = datetime.now(timezone.utc).isoformat()


def make_transport(calls):
    listing = {"messages": [
        {"message_id": "m1", "from": "Nathan <me@example.com>", "subject": "docs", "preview": "Hi Agent, Can you give", "labels": ["unread"], "timestamp": NOW, "thread_id": "t1"},
        {"message_id": "m2", "from": INBOX, "subject": "Re: docs", "preview": "here", "labels": ["sent"], "timestamp": NOW},
    ]}
    full = {"message_id": "m1", "text": "Hi Agent, Can you give me Other Documents files from M12205? Thanks!"}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content))
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(200, json=listing)
        if request.method == "GET" and request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json=full)
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        if request.method == "POST":
            return httpx.Response(200, json={"message_id": "out1", "thread_id": "t1"})
        return httpx.Response(404)

    settings = SimpleNamespace(agentmail_inbox_id=INBOX, agentmail_api_key="k", agent_address=INBOX, allowed_senders=[])
    t = AgentMailTransport(settings)
    t.client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer k"})
    return t


def test_fetch_uses_full_text_not_preview():
    calls = []
    t = make_transport(calls)
    msgs = t.fetch_unprocessed()
    assert [m.id for m in msgs] == ["m1"]
    assert "M12205" in msgs[0].body
    assert msgs[0].sender == "me@example.com"
    assert ("GET", f"/inboxes/{INBOX}/messages/m1", b"") in calls


def test_reply_with_attachment(tmp_path):
    calls = []
    t = make_transport(calls)
    f = tmp_path / "M12205.zip"
    f.write_bytes(b"PK\x03\x04data")
    out = t.send(OutboundMessage(to="me@example.com", subject="Re: docs", body="done", attachments=[f], in_reply_to="m1"))
    assert out == "out1"
    method, path, content = [c for c in calls if c[0] == "POST"][0]
    assert path == f"/inboxes/{INBOX}/messages/m1/reply"
    payload = json.loads(content)
    assert payload["text"] == "done"
    att = payload["attachments"][0]
    assert att["filename"] == "M12205.zip"
    assert base64.b64decode(att["content"]) == b"PK\x03\x04data"


def test_mark_processed_adds_label():
    calls = []
    t = make_transport(calls)
    t.mark_processed_id("m1")
    method, path, content = calls[-1]
    assert (method, path) == ("PATCH", f"/inboxes/{INBOX}/messages/m1")
    assert json.loads(content) == {"add_labels": [DONE_LABEL]}
