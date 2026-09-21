"""Plain IMAP + SMTP transport. Works with Gmail (app password), Outlook, Fastmail, etc.

Processed state lives in a JSON file next to the data dir, keyed by Message-ID,
so restarting the agent never re-answers an email. Unseen flags are not trusted
because a human opening the mailbox would clear them.
"""
from __future__ import annotations

import email
import imaplib
import json
import mimetypes
import smtplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from .base import InboundMessage, OutboundMessage, Transport


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain, html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html:
                html = text
        return plain or _strip_html(html)
    payload = msg.get_payload(decode=True)
    if payload is None:
        return ""
    text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return text if msg.get_content_type() != "text/html" else _strip_html(text)


def _strip_html(html: str) -> str:
    import re

    html = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return email.utils.unquote(text).replace("&nbsp;", " ").replace("&amp;", "&")


class ImapSmtpTransport(Transport):
    def __init__(self, settings):
        self.s = settings
        self.address = settings.agent_address or settings.mail_user
        self.state_path = Path(settings.data_dir) / "imap_state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._processed: set[str] = set()
        if self.state_path.exists():
            self._processed = set(json.loads(self.state_path.read_text()).get("processed", []))

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port)
        conn.login(self.s.mail_user, self.s.mail_password)
        return conn

    def fetch_unprocessed(self) -> list[InboundMessage]:
        conn = self._imap()
        try:
            conn.select("INBOX")
            since = datetime.now(timezone.utc).strftime("%d-%b-%Y")
            _, data = conn.search(None, f'(SINCE "{since}")')
            ids = data[0].split()
            if not ids:
                _, data = conn.search(None, "UNSEEN")
                ids = data[0].split()
            out: list[InboundMessage] = []
            for uid in ids[-50:]:
                _, raw = conn.fetch(uid, "(RFC822)")
                msg = email.message_from_bytes(raw[0][1])
                mid = msg.get("Message-ID", "").strip() or f"uid:{uid.decode()}"
                if mid in self._processed:
                    continue
                sender = parseaddr(msg.get("From", ""))[1].lower()
                if sender == self.address.lower():
                    continue
                if self.s.allowed_senders and sender not in self.s.allowed_senders:
                    continue
                try:
                    received = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    received = datetime.now(timezone.utc)
                out.append(
                    InboundMessage(
                        id=mid,
                        sender=sender,
                        subject=_decode(msg.get("Subject")),
                        body=_text_body(msg),
                        received=received,
                        message_id_header=mid,
                    )
                )
            out.sort(key=lambda m: m.received)
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def mark_processed(self, message: InboundMessage) -> None:
        self._processed.add(message.id)
        self.state_path.write_text(json.dumps({"processed": sorted(self._processed)}, indent=1))

    def send(self, message: OutboundMessage) -> str:
        em = EmailMessage()
        em["From"] = f"{self.s.agent_display_name} <{self.address}>"
        em["To"] = message.to
        em["Subject"] = message.subject
        if message.in_reply_to:
            em["In-Reply-To"] = message.in_reply_to
            em["References"] = message.in_reply_to
        em.set_content(message.body)
        for path in message.attachments:
            ctype, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            em.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=120) as smtp:
            smtp.starttls()
            smtp.login(self.s.mail_user, self.s.mail_password)
            smtp.send_message(em)
        return em["Message-ID"] or "sent"
