"""Directory-backed mailbox for development and evals.

inbox/<id>.json   -> a request waiting to be handled
done/<id>.json    -> handled requests
outbox/<id>.json  -> replies, with attachments copied next to them
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .base import InboundMessage, OutboundMessage, Transport


class LocalTransport(Transport):
    def __init__(self, root: Path, address: str = "agent@local"):
        self.root = Path(root)
        self.address = address
        for d in ("inbox", "done", "outbox"):
            (self.root / d).mkdir(parents=True, exist_ok=True)

    def drop(self, sender: str, subject: str, body: str) -> InboundMessage:
        """Put a message in the inbox, the way a person emailing the agent would."""
        msg = InboundMessage(
            id=uuid.uuid4().hex[:12],
            sender=sender,
            subject=subject,
            body=body,
            received=datetime.now(timezone.utc),
        )
        (self.root / "inbox" / f"{msg.id}.json").write_text(
            json.dumps({**msg.__dict__, "received": msg.received.isoformat()}, indent=2), encoding="utf-8"
        )
        return msg

    def fetch_unprocessed(self) -> list[InboundMessage]:
        out = []
        for p in sorted((self.root / "inbox").glob("*.json"), key=lambda p: p.stat().st_mtime):
            d = json.loads(p.read_text(encoding="utf-8"))
            d["received"] = datetime.fromisoformat(d["received"])
            out.append(InboundMessage(**d))
        return out

    def mark_processed(self, message: InboundMessage) -> None:
        src = self.root / "inbox" / f"{message.id}.json"
        if src.exists():
            shutil.move(src, self.root / "done" / src.name)

    def send(self, message: OutboundMessage) -> str:
        out_id = uuid.uuid4().hex[:12]
        folder = self.root / "outbox" / out_id
        folder.mkdir(parents=True)
        copied = []
        for a in message.attachments:
            dest = folder / a.name
            shutil.copy(a, dest)
            copied.append(str(dest))
        (folder / "message.json").write_text(
            json.dumps(
                {
                    "id": out_id,
                    "to": message.to,
                    "subject": message.subject,
                    "body": message.body,
                    "attachments": copied,
                    "in_reply_to": message.in_reply_to,
                    "sent": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return out_id

    def sent(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "outbox").glob("*/message.json"), key=lambda p: p.stat().st_mtime):
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out
