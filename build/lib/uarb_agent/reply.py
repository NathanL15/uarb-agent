"""Compose the reply email from scraped facts. Every number here comes from the
site; nothing is generated, so the summary cannot drift from the data."""
from __future__ import annotations

from datetime import date

from .bundler import Bundle
from .models import DocType, FetchResult, MatterMetadata, MatterRequest, ParseStatus

ORDER = [DocType.EXHIBITS, DocType.KEY_DOCUMENTS, DocType.OTHER_DOCUMENTS, DocType.TRANSCRIPTS, DocType.RECORDINGS]


def long_date(d: date | None) -> str:
    return f"{d.strftime('%B')} {d.day}, {d.year}" if d else ""


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def count_sentence(meta: MatterMetadata) -> str:
    have, none = [], []
    for dt in ORDER:
        n = meta.counts.get(dt, 0)
        label = dt.plural_label if n != 1 else dt.singular_label
        if n:
            have.append(f"{n} {label}")
        else:
            none.append(dt.plural_label)
    parts = []
    if have:
        parts.append("I found " + _join(have))
    if none:
        parts.append(("and no " if have else "I found no ") + " or ".join(none))
    total = meta.total_files
    return " ".join(parts) + f", {total} file{'s' if total != 1 else ''} in total."


def about_sentences(meta: MatterMetadata) -> list[str]:
    out = []
    if meta.title:
        out.append(f"{meta.matter} is about the {meta.title}.")
    else:
        out.append(f"{meta.matter} has no title on file.")
    if meta.category and meta.type:
        out.append(f"It relates to {meta.category} within the {meta.type} category.")
    elif meta.type:
        out.append(f"It falls under {meta.type}.")
    status_bits = []
    if meta.status:
        status_bits.append(f"its status is {meta.status}")
    if meta.outcome:
        status_bits.append(f"the outcome was {meta.outcome}")
    if status_bits:
        sentence = " and ".join(status_bits)
        out.append(sentence[0].upper() + sentence[1:] + ".")
    dates = []
    if meta.date_received:
        dates.append(f"an initial filing on {long_date(meta.date_received)}")
    if meta.decision_date:
        label = "a decision" if "decision" in meta.decision_date_label.lower() else "a final filing"
        dates.append(f"{label} on {long_date(meta.decision_date)}")
    if dates:
        out.append("The matter had " + " and ".join(dates) + ".")
    return out


def download_sentence(result: FetchResult, bundle: Bundle) -> str:
    n_listed = result.metadata.counts.get(result.doc_type, len(result.listed))
    n_sent = len(bundle.files)
    label = result.doc_type.plural_label
    if n_listed == 0:
        return f"There are no {label} for this matter, so there is nothing to attach."
    if n_sent == 0:
        return f"I could not download any of the {n_listed} {label}; see the notes below."
    s = f"I downloaded {n_sent} out of the {n_listed} {label} and am attaching them as a ZIP here."
    if len(bundle.parts) > 1:
        s += f" The ZIP is split into {len(bundle.parts)} parts to stay under the attachment limit."
    return s


def compose_success(req: MatterRequest, result: FetchResult, bundle: Bundle, part: int | None = None, total_parts: int | None = None) -> tuple[str, str]:
    meta = result.metadata
    subject = f"{meta.matter} {result.doc_type.plural_label}"
    if part and total_parts and total_parts > 1:
        subject += f" (part {part} of {total_parts})"
    lines = ["Hi,", ""]
    lines.append(" ".join(about_sentences(meta)) + " " + count_sentence(meta) + " " + download_sentence(result, bundle))
    if bundle.files:
        lines += ["", "Attached:"]
        members = bundle.parts[part - 1].members if part else bundle.files
        for f in members:
            when = f" ({f.row.filed_text})" if f.row.filed_text else ""
            lines.append(f"  - {f.row.doc_id}  {f.row.title}{when}")
    notes = list(result.skipped)
    for f in bundle.too_large:
        notes.append(f"{f.row.doc_id} ({f.size / 1_048_576:.1f} MB) is too large to email; download it from the UARB site.")
    if notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in notes]
    lines += ["", f"Source: https://uarb.novascotia.ca/fmi/webd/UARB15 (matter {meta.matter}, {result.doc_type.plural_label} tab). Fetched in {result.duration_s:.0f}s."]
    return subject, "\n".join(lines)


def compose_not_found(matter: str) -> tuple[str, str]:
    return (
        f"{matter} not found",
        "Hi,\n\n"
        f"The UARB Public Documents Database returned \"No Records Found\" for {matter}. "
        "Matter numbers are the letter M followed by five digits, for example M12205. "
        "Please check the number and send it again.",
    )


def compose_clarification(req: MatterRequest, original_subject: str) -> tuple[str, str]:
    subject = f"Re: {original_subject}" if original_subject else "Which matter and documents?"
    if req.status == ParseStatus.NO_MATTER:
        body = (
            "Hi,\n\n"
            "I could not find a matter number in your message. Please include one in the form M12205 "
            "along with the document type you want: Exhibits, Key Documents, Other Documents, Transcripts or Recordings."
        )
    elif req.status == ParseStatus.AMBIGUOUS_MATTER:
        body = (
            "Hi,\n\n"
            f"Your message mentions more than one matter number ({'; '.join(req.notes)}). "
            "Please send one request per matter so I attach the right files."
        )
    else:
        body = (
            "Hi,\n\n"
            f"Which documents from {req.matter} would you like? I can fetch Exhibits, Key Documents, "
            "Other Documents, Transcripts or Recordings (up to 10 files per request)."
        )
    return subject, body


def compose_failure(req: MatterRequest, error: str) -> tuple[str, str]:
    matter = req.matter or "your request"
    return (
        f"{matter}: could not complete",
        "Hi,\n\n"
        f"I ran into a problem while fetching {matter} from the UARB database and could not finish: {error}\n\n"
        "The site may be slow or unavailable. Please try again in a few minutes.",
    )
