from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    uarb_url: str = _env("UARB_URL", "https://uarb.novascotia.ca/fmi/webd/UARB15")
    data_dir: Path = Path(_env("UARB_DATA_DIR", "data"))
    max_docs: int = _int("UARB_MAX_DOCS", 10)
    # Attachment ceiling per email, in MB. Gmail accepts 25; keep headroom for base64 overhead.
    attachment_budget_mb: float = float(_env("UARB_ATTACHMENT_MB", "18"))
    # Stop downloading once this much has been pulled for one request (audio can run to hundreds of MB).
    max_total_mb: float = float(_env("UARB_MAX_TOTAL_MB", "200"))
    headless: bool = _env("UARB_HEADLESS", "1") != "0"
    nav_timeout_s: int = _int("UARB_NAV_TIMEOUT_S", 60)
    download_timeout_s: int = _int("UARB_DOWNLOAD_TIMEOUT_S", 180)

    mail_transport: str = _env("MAIL_TRANSPORT", "local")  # local | imap | agentmail
    poll_seconds: int = _int("MAIL_POLL_SECONDS", 20)
    agent_address: str = _env("MAIL_ADDRESS", "")
    agent_display_name: str = _env("MAIL_DISPLAY_NAME", "UARB Filing Agent")

    imap_host: str = _env("IMAP_HOST", "imap.gmail.com")
    imap_port: int = _int("IMAP_PORT", 993)
    smtp_host: str = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = _int("SMTP_PORT", 587)
    mail_user: str = _env("MAIL_USER", "")
    mail_password: str = _env("MAIL_PASSWORD", "")
    allowed_senders: list[str] = field(default_factory=lambda: [s.strip().lower() for s in _env("MAIL_ALLOWED_SENDERS", "").split(",") if s.strip()])

    agentmail_api_key: str = _env("AGENTMAIL_API_KEY", "")
    agentmail_inbox_id: str = _env("AGENTMAIL_INBOX_ID", "")

    llm_backend: str = _env("LLM_BACKEND", "none")  # none | ollama
    ollama_url: str = _env("OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M")

    @property
    def attachment_budget_bytes(self) -> int:
        return int(self.attachment_budget_mb * 1024 * 1024)

    @property
    def max_total_bytes(self) -> int:
        return int(self.max_total_mb * 1024 * 1024)


settings = Settings()
