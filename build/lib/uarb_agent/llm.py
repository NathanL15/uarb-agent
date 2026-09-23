"""Optional local LLM (Ollama) for requests the rules cannot read.

Runs entirely on this machine, costs nothing, and is only ever asked one narrow
question: which matter number and which document tab does this email want. The
caller validates the answer against the email text, so the model cannot invent a
matter number.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

log = logging.getLogger(__name__)

PROMPT = """You read emails sent to a document-retrieval agent for the Nova Scotia UARB public documents database.
Extract what the sender wants.

Rules:
- "matter" is a matter number written as the letter M followed by five digits (example M12205). Copy it exactly as written in the email; if none is written, use null.
- "doc_types" is a list drawn ONLY from: "Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings".
  Exhibits = evidence and filings by the parties. Key Documents = orders, decisions, notices of hearing.
  Other Documents = correspondence, letters, miscellaneous filings. Transcripts = written record of the hearing.
  Recordings = audio of the hearing.
  If the email does not say which kind, use an empty list.
Answer with JSON only, like {{"matter": "M12205", "doc_types": ["Exhibits"]}}.

Subject: {subject}
Email:
{body}
"""


class OllamaParser:
    def __init__(self, url: str = "http://localhost:11434", model: str = "qwen3:4b-instruct-2507-q4_K_M", timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=3)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            return any(n == self.model or n.split(":")[0] == self.model.split(":")[0] for n in names)
        except Exception:
            return False

    def __call__(self, subject: str, body: str) -> Optional[dict]:
        payload = {
            "model": self.model,
            "prompt": PROMPT.format(subject=subject[:300], body=body[:3000]),
            "stream": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 120},
        }
        r = httpx.post(f"{self.url}/api/generate", json=payload, timeout=self.timeout)
        r.raise_for_status()
        text = r.json().get("response", "")
        return parse_json_answer(text)


def parse_json_answer(text: str) -> Optional[dict]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    out: dict = {"matter": None, "doc_types": []}
    matter = data.get("matter")
    if isinstance(matter, str):
        out["matter"] = matter.strip().upper().replace(" ", "").replace("-", "")
    types = data.get("doc_types")
    if isinstance(types, str):
        types = [types]
    if isinstance(types, list):
        out["doc_types"] = [str(t).strip() for t in types if str(t).strip()]
    return out
