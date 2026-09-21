from .base import InboundMessage, OutboundMessage, Transport
from .local import LocalTransport


def build_transport(settings) -> Transport:
    kind = settings.mail_transport
    if kind == "local":
        return LocalTransport(settings.data_dir / "mailbox", address=settings.agent_address or "agent@local")
    if kind == "imap":
        from .imap_smtp import ImapSmtpTransport

        return ImapSmtpTransport(settings)
    if kind == "agentmail":
        from .agentmail import AgentMailTransport

        return AgentMailTransport(settings)
    raise ValueError(f"unknown MAIL_TRANSPORT {kind!r}")


__all__ = ["InboundMessage", "OutboundMessage", "Transport", "LocalTransport", "build_transport"]
