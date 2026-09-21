from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class InboundMessage:
    id: str
    sender: str
    subject: str
    body: str
    received: datetime
    thread_id: str = ""
    message_id_header: str = ""


@dataclass
class OutboundMessage:
    to: str
    subject: str
    body: str
    attachments: list[Path] = field(default_factory=list)
    in_reply_to: str = ""
    thread_id: str = ""


class Transport(ABC):
    address: str

    @abstractmethod
    def fetch_unprocessed(self) -> list[InboundMessage]:
        """Return messages the agent has not handled yet, oldest first."""

    @abstractmethod
    def mark_processed(self, message: InboundMessage) -> None:
        ...

    @abstractmethod
    def send(self, message: OutboundMessage) -> str:
        """Send and return an identifier for the outgoing message."""
