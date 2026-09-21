"""Rules that stop the agent from talking to robots or being flooded.

Shared by every transport so the behaviour is identical whichever mailbox is in
front of the agent.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

ROBOT_LOCALPARTS = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce|bounces|notifications?|noreply-.*|.*-noreply)$", re.I)
AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "x-auto-response-suppress", "precedence", "list-id", "list-unsubscribe")


def looks_automated(sender: str, headers: dict[str, str], subject: str = "") -> str | None:
    """Return a reason if this message should never get a reply, else None."""
    local = sender.split("@", 1)[0] if sender else ""
    if not sender or "@" not in sender:
        return "no usable sender"
    if ROBOT_LOCALPARTS.match(local):
        return f"sender {local} is a robot address"
    lowered = {k.lower(): (v or "").lower() for k, v in headers.items()}
    if lowered.get("auto-submitted", "no") not in ("", "no"):
        return "auto-submitted"
    if lowered.get("precedence") in ("bulk", "list", "junk"):
        return f"precedence {lowered['precedence']}"
    for h in ("x-autoreply", "x-autorespond", "list-id", "list-unsubscribe"):
        if h in lowered:
            return f"has {h}"
    if lowered.get("x-auto-response-suppress"):
        return "auto-response-suppress"
    if re.search(r"^(auto(matic)?[ -]?reply|out of (the )?office|delivery (status|failure)|undeliverable)", subject or "", re.I):
        return "auto-reply subject"
    return None


class RateLimiter:
    """At most `limit` replies per sender per `window` seconds; a global cap on top."""

    def __init__(self, per_sender: int = 10, global_limit: int = 60, window_s: int = 3600, clock=time.monotonic):
        self.per_sender = per_sender
        self.global_limit = global_limit
        self.window = window_s
        self.clock = clock
        self._by_sender: dict[str, deque] = defaultdict(deque)
        self._all: deque = deque()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] > self.window:
            q.popleft()

    def allow(self, sender: str) -> bool:
        now = self.clock()
        q = self._by_sender[sender.lower()]
        self._trim(q, now)
        self._trim(self._all, now)
        if len(q) >= self.per_sender or len(self._all) >= self.global_limit:
            return False
        q.append(now)
        self._all.append(now)
        return True


def too_old(received: datetime, max_age: timedelta = timedelta(days=3)) -> bool:
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - received > max_age


def old_enough(received: datetime, min_age_s: int) -> bool:
    """For a backup worker: only touch mail the primary has had `min_age_s` to answer."""
    if min_age_s <= 0:
        return True
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - received >= timedelta(seconds=min_age_s)
