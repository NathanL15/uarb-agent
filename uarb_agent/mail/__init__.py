from .base import InboundMessage, OutboundMessage, Transport
from .local import LocalTransport


def build_transport(settings, min_age_s: int = 0) -> Transport:
    """`min_age_s` > 0 turns a worker into a backup that only takes mail the
    primary has had that long to answer."""
    kind = settings.mail_transport
    if kind == "local":
        return LocalTransport(settings.data_dir / "mailbox", address=settings.agent_address or "agent@local")
    if kind == "imap":
        from .imap_smtp import ImapSmtpTransport

        return ImapSmtpTransport(settings, min_age_s=min_age_s)
    if kind == "agentmail":
        from .agentmail import AgentMailTransport

        return AgentMailTransport(settings, min_age_s=min_age_s)
    raise ValueError(f"unknown MAIL_TRANSPORT {kind!r}")


__all__ = ["InboundMessage", "OutboundMessage", "Transport", "LocalTransport", "build_transport"]
