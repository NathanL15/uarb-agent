"""The loop: read requests, fetch from UARB, reply.

Each inbound message becomes one run folder under data/runs/<id>/ holding the
parsed request, the downloaded files, the ZIP(s) and the reply text, so any
answer the agent sent can be reproduced and audited afterwards.

The loop is written to be left alone for weeks: the browser is recycled after
failures, a heartbeat file is touched every poll, the machine is kept awake
while it runs, and a change to .env makes the process exit with code 3 so the
supervisor around it restarts with the new settings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .bundler import Bundle, bundle
from .config import Settings
from .mail import InboundMessage, OutboundMessage, Transport
from .mail.guards import RateLimiter
from .models import FetchResult, MatterNotFound, MatterRequest, ParseStatus
from .reply import compose_clarification, compose_failure, compose_not_found, compose_success
from .request_parser import parse_request
from .uarb import UarbClient

log = logging.getLogger(__name__)
RESTART_EXIT_CODE = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Agent:
    def __init__(self, settings: Settings, transport: Transport, llm: Optional[Callable] = None):
        self.s = settings
        self.transport = transport
        self.llm = llm
        self.runs_dir = Path(settings.data_dir) / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path = Path(settings.data_dir) / "heartbeat.json"
        self.limiter = RateLimiter(per_sender=settings.max_replies_per_sender_hour, global_limit=settings.max_replies_per_hour)
        self.handled = 0
        self.failures = 0

    # -- one message -------------------------------------------------------------

    async def handle(self, msg: InboundMessage, client: UarbClient) -> dict:
        run_dir = self.runs_dir / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{_slug(msg.id)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        record: dict = {"message_id": msg.id, "from": msg.sender, "subject": msg.subject, "started": _now(), "steps": []}
        step = lambda text: (record["steps"].append({"t": _now(), "text": text}), log.info("[%s] %s", msg.id, text))

        if not self.limiter.allow(msg.sender):
            step("rate limit reached for this sender; not replying")
            record["outcome"] = "rate_limited"
            record["finished"] = _now()
            (run_dir / "run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
            return record

        req = parse_request(msg.body, msg.subject, llm=self.llm)
        record["request"] = req.model_dump(mode="json")
        step(f"parsed: matter={req.matter} types={[t.value for t in req.doc_types]} status={req.status.value} via {req.source}")

        replies: list[OutboundMessage] = []
        reply_to = msg.message_id_header or msg.id
        if req.status != ParseStatus.OK:
            subject, body = compose_clarification(req, msg.subject)
            replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=reply_to, thread_id=msg.thread_id))
            record["outcome"] = "clarification"
        else:
            for doc_type in req.doc_types:
                try:
                    result = await client.fetch(req.matter, doc_type, limit=req.limit, max_total_bytes=self.s.max_total_bytes, max_file_bytes=self.s.attachment_budget_bytes, on_progress=step)
                except MatterNotFound:
                    subject, body = compose_not_found(req.matter)
                    replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=reply_to, thread_id=msg.thread_id))
                    record["outcome"] = "not_found"
                    break
                except Exception as exc:
                    log.exception("fetch failed")
                    self.failures += 1
                    subject, body = compose_failure(req, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")
                    replies.append(OutboundMessage(to=msg.sender, subject=subject, body=body, in_reply_to=reply_to, thread_id=msg.thread_id))
                    record["outcome"] = "error"
                    continue
                self.failures = 0
                record.setdefault("results", []).append(_result_summary(result))
                zips = bundle(result.downloaded, run_dir, f"{req.matter} {doc_type.value}", budget_bytes=self.s.attachment_budget_bytes)
                step(f"{doc_type.value}: listed {len(result.listed)}, downloaded {len(result.downloaded)}, zip parts {len(zips.parts)}, too large {len(zips.too_large)}")
                replies.extend(self._success_replies(msg, req, result, zips))
                record.setdefault("outcome", "sent")

        for i, r in enumerate(replies, 1):
            out_id = self._send_with_retry(r)
            (run_dir / f"reply-{i}.txt").write_text(f"To: {r.to}\nSubject: {r.subject}\nAttachments: {[a.name for a in r.attachments]}\n\n{r.body}", encoding="utf-8")
            step(f"sent reply {i}/{len(replies)} ({out_id}): {r.subject}")
        record["finished"] = _now()
        (run_dir / "run.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        self.handled += 1
        return record

    def _send_with_retry(self, message: OutboundMessage, attempts: int = 3) -> str:
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self.transport.send(message)
            except Exception as exc:
                last = exc
                log.warning("send failed (%d/%d): %s", i + 1, attempts, exc)
                import time

                time.sleep(5 * (i + 1))
        raise RuntimeError(f"could not send reply: {last}")

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

    def _client(self) -> UarbClient:
        return UarbClient(self.s.uarb_url, Path(self.s.data_dir) / "downloads", headless=self.s.headless, nav_timeout_s=self.s.nav_timeout_s, download_timeout_s=self.s.download_timeout_s)

    def _heartbeat(self, state: str) -> None:
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.write_text(json.dumps({"t": _now(), "state": state, "handled": self.handled, "failures": self.failures, "transport": self.s.mail_transport, "address": self.transport.address, "pid": os.getpid()}), encoding="utf-8")
        except Exception:
            pass

    async def run_forever(self, stop_after: int | None = None) -> int:
        """Poll until stopped. Returns an exit code for the supervisor."""
        env_stamp = _env_mtime()
        keep_awake = _KeepAwake(enabled=self.s.keep_awake)
        keep_awake.start()
        handled_at_start = self.handled
        try:
            while True:
                async with self._client() as client:
                    log.info("listening on %s (transport=%s, poll=%ss)", self.transport.address, self.s.mail_transport, self.s.poll_seconds)
                    failures_this_browser = 0
                    while True:
                        self._heartbeat("polling")
                        if _env_mtime() != env_stamp:
                            log.info(".env changed; exiting so the supervisor restarts with the new settings")
                            return RESTART_EXIT_CODE
                        try:
                            pending = self.transport.fetch_unprocessed()
                        except Exception:
                            log.exception("could not read inbox; retrying next poll")
                            pending = []
                        for msg in pending:
                            log.info("request from %s: %r", msg.sender, msg.subject)
                            # Mark first so a crash mid-way never causes a duplicate reply storm.
                            self.transport.mark_processed(msg)
                            self._heartbeat(f"handling {msg.id}")
                            before = self.failures
                            try:
                                await self.handle(msg, client)
                            except Exception:
                                log.exception("unhandled error for %s", msg.id)
                                self.failures += 1
                            if self.failures > before:
                                failures_this_browser += 1
                            if stop_after and self.handled - handled_at_start >= stop_after:
                                return 0
                        if failures_this_browser >= 2:
                            log.warning("two failures in a row; recycling the browser")
                            break
                        await asyncio.sleep(self.s.poll_seconds)
        finally:
            keep_awake.stop()
            self._heartbeat("stopped")

    async def run_once(self) -> int:
        """Handle whatever is waiting now, then return how many were handled."""
        pending = self.transport.fetch_unprocessed()
        if not pending:
            self._heartbeat("idle")
            return 0
        async with self._client() as client:
            for msg in pending:
                self.transport.mark_processed(msg)
                await self.handle(msg, client)
        self._heartbeat("idle")
        return len(pending)


class _KeepAwake:
    """Hold a Windows power request so a laptop does not sleep under the agent."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_AWAYMODE_REQUIRED = 0x00000040

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.platform == "win32"

    def start(self) -> None:
        if not self.enabled:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED | self.ES_AWAYMODE_REQUIRED)
            log.info("holding a system power request so the machine stays awake")
        except Exception as exc:
            log.warning("could not set the power request: %s", exc)

    def stop(self) -> None:
        if not self.enabled:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        except Exception:
            pass


def _env_mtime() -> float | None:
    p = Path(".env")
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return None


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
