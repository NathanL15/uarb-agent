"""Turn a free-text email into a MatterRequest.

Rules first, because they are cheap, deterministic and testable. An LLM is only
consulted for phrasing the rules cannot resolve, and its answer is checked against
the same constraints (the matter id has to appear in the text, the type has to be
one of the five tabs).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from .models import DocType, MatterRequest, ParseStatus

# M followed by five digits; tolerate "M 12205", "m12205", "M-12205", "matter #12205".
MATTER_RE = re.compile(r"\b[Mm]\s?-?\s?(\d{5})\b")
BARE_MATTER_RE = re.compile(r"\bmatter(?:\s+(?:number|no\.?|#))?\s*[:#]?\s*(\d{5})\b", re.I)
LIMIT_RE = re.compile(r"\b(?:first|top|up to|at most|max(?:imum)?|only|just|send|give(?: me)?|grab|get|fetch|need|want)\s+(\d{1,3})\s+(?:files?|docs?|documents?|exhibits?|transcripts?|recordings?|items?)\b", re.I)

# Each entry: (pattern, type). Order matters only for readability; all are checked.
TYPE_PATTERNS: list[tuple[re.Pattern[str], DocType]] = [
    (re.compile(r"\bkey\s*(?:docs?|documents?)\b", re.I), DocType.KEY_DOCUMENTS),
    (re.compile(r"\b(?:orders?|decisions?|rulings?|notices? of (?:hearing|application))\b", re.I), DocType.KEY_DOCUMENTS),
    (re.compile(r"\bother\s*(?:docs?|documents?|files?)\b", re.I), DocType.OTHER_DOCUMENTS),
    (re.compile(r"\b(?:correspondence|letters?|misc(?:ellaneous)?(?: docs| documents| files)?)\b", re.I), DocType.OTHER_DOCUMENTS),
    (re.compile(r"\bexhibits?\b", re.I), DocType.EXHIBITS),
    (re.compile(r"\b(?:evidence|filings? by (?:the )?parties|party filings?|submissions?)\b", re.I), DocType.EXHIBITS),
    (re.compile(r"\btranscripts?\b", re.I), DocType.TRANSCRIPTS),
    (re.compile(r"\b(?:hearing text|written record of the hearing)\b", re.I), DocType.TRANSCRIPTS),
    (re.compile(r"\brecordings?\b", re.I), DocType.RECORDINGS),
    (re.compile(r"\b(?:audio(?: files?)?|mp3s?|sound files?)\b", re.I), DocType.RECORDINGS),
]

# Plain "documents"/"files" on its own says nothing about which tab; treat as unknown.
QUOTED_REPLY_RE = re.compile(r"^\s*(?:On .{5,120} wrote:|-{2,}\s*Original Message|From: .+)\s*$", re.M)


def strip_quoted_reply(text: str) -> str:
    """Drop quoted history so a reply chain does not re-trigger old requests."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    text = "\n".join(lines)
    m = QUOTED_REPLY_RE.search(text)
    if m:
        text = text[: m.start()]
    return text


def find_matters(text: str) -> list[str]:
    found: list[str] = []
    for m in MATTER_RE.finditer(text):
        found.append(f"M{m.group(1)}")
    for m in BARE_MATTER_RE.finditer(text):
        found.append(f"M{m.group(1)}")
    seen: list[str] = []
    for f in found:
        if f not in seen:
            seen.append(f)
    return seen


def find_doc_types(text: str) -> list[DocType]:
    hits: list[tuple[int, DocType]] = []
    for pat, dt in TYPE_PATTERNS:
        for m in pat.finditer(text):
            hits.append((m.start(), dt))
    hits.sort()
    out: list[DocType] = []
    for _, dt in hits:
        if dt not in out:
            out.append(dt)
    return out


def find_limit(text: str, default: int = 10) -> int:
    m = LIMIT_RE.search(text)
    if not m:
        return default
    n = int(m.group(1))
    return max(1, min(n, default))


def parse_with_rules(text: str, subject: str = "") -> MatterRequest:
    """The body is what the person is asking now; the subject only fills gaps,
    because in a reply chain it still carries the previous request."""
    body = strip_quoted_reply(text or "")
    subject = subject or ""
    matters = find_matters(body) or find_matters(subject)
    types = find_doc_types(body) or find_doc_types(subject)
    limit = find_limit(body) if find_limit(body) != 10 else find_limit(subject)
    req = MatterRequest(doc_types=types, limit=limit, source="rules")
    if not matters:
        req.status = ParseStatus.NO_MATTER
    elif len(matters) > 1:
        req.status = ParseStatus.AMBIGUOUS_MATTER
        req.notes.append("several matter numbers mentioned: " + ", ".join(matters))
        req.matter = matters[0]
    else:
        req.matter = matters[0]
    if req.status == ParseStatus.OK and not types:
        req.status = ParseStatus.NO_DOC_TYPE
    return req


LLMParser = Callable[[str, str], Optional[dict]]


def parse_request(text: str, subject: str = "", llm: Optional[LLMParser] = None) -> MatterRequest:
    """Rules, then the LLM only for what the rules left unresolved."""
    req = parse_with_rules(text, subject)
    # Two matter numbers in one email is a question for the sender, not the model.
    if req.status in (ParseStatus.OK, ParseStatus.AMBIGUOUS_MATTER) or llm is None:
        return req
    body = strip_quoted_reply(text or "")
    try:
        guess = llm(subject or "", body)
    except Exception as exc:  # the LLM is optional; never let it break the pipeline
        req.notes.append(f"llm unavailable: {exc}")
        return req
    if not guess:
        return req
    matters_in_text = set(find_matters(f"{subject}\n{body}"))
    matter = guess.get("matter")
    if isinstance(matter, str):
        matter = matter.strip().upper().replace(" ", "")
        if re.fullmatch(r"M\d{5}", matter) and matter in matters_in_text:
            if req.status == ParseStatus.NO_MATTER:
                req.matter = matter
                req.status = ParseStatus.OK if req.doc_types else ParseStatus.NO_DOC_TYPE
                req.source = "rules+llm"
    types = guess.get("doc_types") or []
    picked: list[DocType] = []
    for t in types:
        try:
            dt = DocType(str(t).strip())
        except ValueError:
            continue
        if dt not in picked:
            picked.append(dt)
    if picked and not req.doc_types:
        req.doc_types = picked
        req.source = "rules+llm"
        if req.status == ParseStatus.NO_DOC_TYPE:
            req.status = ParseStatus.OK
    return req
