"""Plain IMAP + SMTP transport. Works with Gmail (app password), Outlook, Fastmail, etc.

"Processed" is recorded on the server: a handled message is moved out of INBOX
into the `MAIL_DONE_FOLDER` mailbox. That way any number of workers (the laptop,
a VM, a scheduled job) can share one inbox without a shared database, and a
restart never re-answers anything. Unseen flags are not used because a human
opening the mailbox would clear them.
"""
from __future__ import annotations

import email
import imaplib
import logging
import mimetypes
import re
import smtplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr, parsedate_to_datetime

from .base import InboundMessage, OutboundMessage, Transport
from .guards import looks_automated, old_enough, too_old

log = logging.getLogger(__name__)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _strip_html(html: str) -> str:
    html = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain, html = "", ""
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            ctype = part.get_content_type()
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


class ImapSmtpTransport(Transport):
    def __init__(self, settings, min_age_s: int = 0):
        self.s = settings
        self.address = (settings.agent_address or settings.mail_user).lower()
        self.done_folder = getattr(settings, "mail_done_folder", "UARB-Agent-Done") or "UARB-Agent-Done"
        self.min_age_s = min_age_s
        self._uids: dict[str, bytes] = {}

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port)
        conn.login(self.s.mail_user, self.s.mail_password)
        return conn

    def _ensure_done_folder(self, conn: imaplib.IMAP4_SSL) -> None:
        typ, _ = conn.select(f'"{self.done_folder}"', readonly=True)
        if typ != "OK":
            conn.create(f'"{self.done_folder}"')
            try:
                conn.subscribe(f'"{self.done_folder}"')
            except Exception:
                pass

    def fetch_unprocessed(self) -> list[InboundMessage]:
        conn = self._imap()
        try:
            self._ensure_done_folder(conn)
            conn.select("INBOX")
            _, data = conn.uid("search", None, "ALL")
            uids = data[0].split()
            out: list[InboundMessage] = []
            for uid in uids[-100:]:
                _, raw = conn.uid("fetch", uid, "(RFC822)")
                if not raw or raw[0] is None:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                mid = (msg.get("Message-ID") or "").strip() or f"uid:{uid.decode()}"
                sender = parseaddr(msg.get("From", ""))[1].lower()
                if sender == self.address:
                    self._move_to_done(conn, uid)
                    continue
                reason = looks_automated(sender, {k: v for k, v in msg.items()}, _decode(msg.get("Subject")))
                if reason:
                    log.info("ignoring %s from %s: %s", mid, sender, reason)
                    self._move_to_done(conn, uid)
                    continue
                if self.s.allowed_senders and sender not in self.s.allowed_senders:
                    log.info("ignoring %s from %s: not in MAIL_ALLOWED_SENDERS", mid, sender)
                    self._move_to_done(conn, uid)
                    continue
                try:
                    received = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    received = datetime.now(timezone.utc)
                if too_old(received):
                    log.info("ignoring %s: older than three days", mid)
                    self._move_to_done(conn, uid)
                    continue
                if not old_enough(received, self.min_age_s):
                    continue
                self._uids[mid] = uid
                out.append(InboundMessage(id=mid, sender=sender, subject=_decode(msg.get("Subject")), body=_text_body(msg), received=received, message_id_header=mid))
            out.sort(key=lambda m: m.received)
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _move_to_done(self, conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
        conn.uid("copy", uid, f'"{self.done_folder}"')
        conn.uid("store", uid, "+FLAGS", r"(\Deleted \Seen)")
        conn.expunge()

    def mark_processed(self, message: InboundMessage) -> None:
        uid = self._uids.pop(message.id, None)
        conn = self._imap()
        try:
            conn.select("INBOX")
            if uid is None:
                _, data = conn.uid("search", None, f'(HEADER Message-ID "{message.id}")')
                found = data[0].split()
                if not found:
                    return
                uid = found[-1]
            self._move_to_done(conn, uid)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def send(self, message: OutboundMessage) -> str:
        em = EmailMessage()
        em["From"] = f"{self.s.agent_display_name} <{self.address}>"
        em["To"] = message.to
        em["Subject"] = message.subject
        em["Message-ID"] = make_msgid(domain=self.address.split("@", 1)[-1] or None)
        em["Auto-Submitted"] = "auto-replied"
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
        return em["Message-ID"]
