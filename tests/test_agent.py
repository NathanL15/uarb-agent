"""End-to-end through the agent with a fake UARB client, so the email side is
checked without a browser or a network."""
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from conftest import make_files

from uarb_agent.agent import Agent
from uarb_agent.config import Settings
from uarb_agent.mail import LocalTransport
from uarb_agent.models import DocType, FetchResult, MatterNotFound


class FakeClient:
    def __init__(self, meta, tmp_path, sizes=None, fail=None):
        self.meta = meta
        self.tmp_path = tmp_path
        self.sizes = sizes or [1000, 1000, 1000]
        self.fail = fail
        self.calls = []

    async def fetch(self, matter, doc_type, limit=10, max_total_bytes=None, max_file_bytes=None, on_progress=None):
        self.calls.append((matter, doc_type, limit))
        if self.fail:
            raise self.fail
        if matter != "M12205":
            raise MatterNotFound(matter)
        n = min(limit, self.meta.counts.get(doc_type, 0), len(self.sizes))
        files = make_files(self.tmp_path / doc_type.value.replace(" ", "_"), self.sizes[:n])
        listed = [f.row for f in files]
        return FetchResult(metadata=self.meta, doc_type=doc_type, listed=listed, downloaded=files, started=datetime.now(timezone.utc), finished=datetime.now(timezone.utc))


def make_agent(tmp_path, client_kwargs=None, meta=None, budget_mb=18):
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.attachment_budget_mb = budget_mb
    t = LocalTransport(s.data_dir / "mailbox", address="agent@test")
    return Agent(s, t), t


def run(agent, transport, client, sender, subject, body):
    msg = transport.drop(sender, subject, body)
    transport.mark_processed(msg)
    return asyncio.run(agent.handle(msg, client))


def test_happy_path(tmp_path, meta):
    agent, t = make_agent(tmp_path)
    client = FakeClient(meta, tmp_path)
    rec = run(agent, t, client, "nathan@example.com", "", "Hi Agent, Can you give me Other Documents files from M12205? Thanks!")
    assert client.calls == [("M12205", DocType.OTHER_DOCUMENTS, 10)]
    sent = t.sent()
    assert len(sent) == 1
    assert sent[0]["to"] == "nathan@example.com"
    assert sent[0]["subject"] == "M12205 Other Documents"
    assert sent[0]["attachments"][0].endswith("M12205 Other Documents.zip")
    assert "I downloaded 3 out of the 43 Other Documents" in sent[0]["body"]
    assert rec["outcome"] == "sent"
    run_json = list((agent.runs_dir).glob("*/run.json"))
    assert len(run_json) == 1
    data = json.loads(run_json[0].read_text())
    assert data["results"][0]["downloaded"][0]["doc_id"] == "100000"


def test_not_found(tmp_path, meta):
    agent, t = make_agent(tmp_path)
    run(agent, t, FakeClient(meta, tmp_path), "a@b.c", "", "exhibits for M99999")
    sent = t.sent()
    assert sent[0]["subject"] == "M99999 not found"
    assert sent[0]["attachments"] == []


def test_clarification_when_no_matter(tmp_path, meta):
    agent, t = make_agent(tmp_path)
    client = FakeClient(meta, tmp_path)
    run(agent, t, client, "a@b.c", "help", "send me the exhibits")
    assert client.calls == []
    assert "could not find a matter number" in t.sent()[0]["body"]


def test_two_types_two_replies(tmp_path, meta):
    agent, t = make_agent(tmp_path)
    run(agent, t, FakeClient(meta, tmp_path), "a@b.c", "", "M12205 exhibits and key documents")
    subjects = [m["subject"] for m in t.sent()]
    assert subjects == ["M12205 Exhibits", "M12205 Key Documents"]


def test_split_parts(tmp_path, meta):
    agent, t = make_agent(tmp_path, budget_mb=1)
    client = FakeClient(meta, tmp_path, sizes=[400_000, 400_000, 400_000])
    run(agent, t, client, "a@b.c", "", "M12205 other documents")
    sent = t.sent()
    assert [m["subject"] for m in sent] == ["M12205 Other Documents (part 1 of 2)", "M12205 Other Documents (part 2 of 2)"]
    assert all(len(m["attachments"]) == 1 for m in sent)


def test_site_failure_gets_an_apology(tmp_path, meta):
    agent, t = make_agent(tmp_path)
    client = FakeClient(meta, tmp_path, fail=RuntimeError("could not open M12205 after 3 attempts"))
    rec = run(agent, t, client, "a@b.c", "", "M12205 transcripts")
    assert rec["outcome"] == "error"
    assert "could not finish" in t.sent()[0]["body"]


def test_run_once_marks_processed_before_handling(tmp_path, meta, monkeypatch):
    agent, t = make_agent(tmp_path)
    t.drop("a@b.c", "", "M12205 exhibits")

    class Ctx:
        async def __aenter__(self):
            return FakeClient(meta, tmp_path)

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("uarb_agent.agent.UarbClient", lambda *a, **k: Ctx())
    assert asyncio.run(agent.run_once()) == 1
    assert t.fetch_unprocessed() == []
    assert asyncio.run(agent.run_once()) == 0
