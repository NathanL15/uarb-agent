"""The loop: read requests, fetch from UARB, reply.

Each inbound message becomes one run folder under data/runs/<id>/ holding the
parsed request, the downloaded files, the ZIP(s) and the reply text, so any
answer the agent sent can be reproduced and audited afterwards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .bundler import Bundle, bundle
from .config import Settings
from .mail import InboundMessage, OutboundMessage, Transport
from .models import DocType, FetchResult, MatterNotFound, MatterRequest, ParseStatus
from .reply import compose_clarification, compose_failure, compose_not_found, compose_success
from .request_parser import parse_request
from .uarb import UarbClient

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Agent:
    def __init__(self, settings: Settings, transport: Transport, llm: Optional[Callable] = None):
        self.s = settings
        self.transport = transport
        self.llm = llm
        self.runs_dir = Path(settings.data_dir) / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    # -- one message -------------------------------------------------------------

    async def handle(self, msg: InboundMessage, client: UarbClient) -> dict:
        run_dir = self.runs_dir / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{_slug(msg.id)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        record: dict = {"message_id": msg.id, "from": msg.sender, "subject": msg.subject, "started": _now(), "steps": []}
        step = lambda text: (record["steps"].append({"t": _now(), "text": text}), log.info("[%s] %s", msg.id, text))

        req = parse_request(msg.body, msg.subject, llm=self.llm)
        record["request"] = req.model_dump(mode="json")
        step(f"parsed: matter={req.matter} types={[t.value for t in req.doc_types]} status={req.status.value} via {req.source}")

        replies: list[OutboundMessage] = []
        if req.status != ParseStatus.OK:
            subject, body = compose_clarification(req, msg.subject)
            replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=msg.message_id_header or msg.id, thread_id=msg.thread_id))
            record["outcome"] = "clarification"
        else:
            for doc_type in req.doc_types:
                try:
                    result = await client.fetch(req.matter, doc_type, limit=req.limit, max_total_bytes=self.s.max_total_bytes, max_file_bytes=self.s.attachment_budget_bytes, on_progress=step)
                except MatterNotFound:
                    subject, body = compose_not_found(req.matter)
                    replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=msg.message_id_header or msg.id, thread_id=msg.thread_id))
                    record["outcome"] = "not_found"
                    break
                except Exception as exc:
                    log.exception("fetch failed")
                    subject, body = compose_failure(req, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
                    replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=msg.message_id_header or msg.id, thread_id=msg.thread_id))
                    record["outcome"] = "error"
                    continue
                record.setdefault("results", []).append(_result_summary(result))
                zips = bundle(result.downloaded, run_dir, f"{req.matter} {doc_type.value}", budget_bytes=self.s.attachment_budget_bytes)
                step(f"{doc_type.value}: listed {len(result.listed)}, downloaded {len(result.downloaded)}, zip parts {len(zips.parts)}, too large {len(zips.too_large)}")
                replies.extend(self._success_replies(msg, req, result, zips))
                record.setdefault("outcome", "sent")

        for i, r in enumerate(replies, 1):
            out_id = self.transport.send(r)
            (run_dir / f"reply-{i}.txt").write_text(f"To: {r.to}\nSubject: {r.subject}\nAttachments: {[a.name for a in r.attachments]}\n\n{r.body}", encoding="utf-8")
            step(f"sent reply {i}/{len(replies)} ({out_id}): {r.subject}")
        record["finished"] = _now()
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        return record

    def _success_replies(self, msg: InboundMessage, req: MatterRequest, result: FetchResult, zips: Bundle) -> list[OutboundMessage]:
        reply_to = msg.message_id_header or msg.id
        if not zips.parts:
            subject, body = compose_success(req, result, zips)
            return [OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=reply_to, thread_id=msg.thread_id)]
        out = []
        n = len(zips.parts)
        for i, part in enumerate(zips.parts, 1):
            subject, body = compose_success(req, result, zips, part=i, total_parts=n)
            out.append(OutboundMessage(to=msg.sender, subject=subject, body=body, attachments=[part.path], in_reply_to=reply_to, thread_id=msg.thread_id))
        return out

    # -- the loop ------------------------------------------------------------------

    async def run_forever(self, stop_after: int | None = None) -> None:
        handled = 0
        async with UarbClient(self.s.uarb_url, Path(self.s.data_dir) / "downloads", headless=self.s.headless, nav_timeout_s=self.s.nav_timeout_s, download_timeout_s=self.s.download_timeout_s) as client:
            log.info("listening on %s (transport=%s, poll=%ss)", self.transport.address, self.s.mail_transport, self.s.poll_seconds)
            while True:
                try:
                    pending = self.transport.fetch_unprocessed()
                except Exception:
                    log.exception("could not read inbox; retrying")
                    pending = []
                for msg in pending:
                    log.info("request from %s: %r", msg.sender, msg.subject)
                    # Mark first so a crash mid-way never causes a duplicate reply storm.
                    self.transport.mark_processed(msg)
                    try:
                        await self.handle(msg, client)
                    except Exception:
                        log.exception("unhandled error for %s", msg.id)
                    handled += 1
                    if stop_after and handled >= stop_after:
                        return
                await asyncio.sleep(self.s.poll_seconds)

    async def run_once(self) -> int:
        """Handle whatever is waiting now, then return how many were handled."""
        pending = self.transport.fetch_unprocessed()
        if not pending:
            return 0
        async with UarbClient(self.s.uarb_url, Path(self.s.data_dir) / "downloads", headless=self.s.headless, nav_timeout_s=self.s.nav_timeout_s, download_timeout_s=self.s.download_timeout_s) as client:
            for msg in pending:
                self.transport.mark_processed(msg)
                await self.handle(msg, client)
        return len(pending)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:24].strip("-") or "msg"


def _result_summary(r: FetchResult) -> dict:
    return {
        "doc_type": r.doc_type.value,
        "metadata": r.metadata.model_dump(mode="json"),
        "listed": [d.model_dump(mode="json") for d in r.listed],
        "downloaded": [{"doc_id": f.row.doc_id, "file": f.path.name, "size": f.size} for f in r.downloaded],
        "skipped": r.skipped,
        "seconds": round(r.duration_s, 1),
    }
