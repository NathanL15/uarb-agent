import pytest

from uarb_agent.models import DocType, ParseStatus
from uarb_agent.request_parser import parse_request, parse_with_rules, strip_quoted_reply


def test_example_from_brief():
    req = parse_with_rules("Hi Agent, Can you give me Other Documents files from M12205? Thanks!")
    assert req.status == ParseStatus.OK
    assert req.matter == "M12205"
    assert req.doc_types == [DocType.OTHER_DOCUMENTS]
    assert req.limit == 10


@pytest.mark.parametrize(
    "text,matter",
    [
        ("m12383 exhibits please", "M12383"),
        ("Matter M 12205 - transcripts", "M12205"),
        ("matter number 12205, key documents", "M12205"),
        ("Matter #12205 recordings", "M12205"),
        ("M-12205 exhibits", "M12205"),
    ],
)
def test_matter_spellings(text, matter):
    assert parse_with_rules(text).matter == matter


@pytest.mark.parametrize(
    "text,expected",
    [
        ("send me the exhibits for M12205", [DocType.EXHIBITS]),
        ("key docs from M12205", [DocType.KEY_DOCUMENTS]),
        ("the audio for M12205", [DocType.RECORDINGS]),
        ("transcript of the hearing, M12205", [DocType.TRANSCRIPTS]),
        ("M12205 other docs", [DocType.OTHER_DOCUMENTS]),
        ("M12205: exhibits and transcripts", [DocType.EXHIBITS, DocType.TRANSCRIPTS]),
        ("decisions and orders for M12205", [DocType.KEY_DOCUMENTS]),
    ],
)
def test_doc_type_phrasings(text, expected):
    assert parse_with_rules(text).doc_types == expected


def test_subject_line_counts():
    req = parse_with_rules("please and thank you", subject="M12205 exhibits")
    assert req.status == ParseStatus.OK
    assert req.matter == "M12205"


def test_missing_matter():
    req = parse_with_rules("Can I get the exhibits?")
    assert req.status == ParseStatus.NO_MATTER


def test_missing_type():
    req = parse_with_rules("What do you have on M12205?")
    assert req.status == ParseStatus.NO_DOC_TYPE
    assert req.matter == "M12205"


def test_two_matters_flagged():
    req = parse_with_rules("exhibits for M12205 and M12383")
    assert req.status == ParseStatus.AMBIGUOUS_MATTER


def test_limit_phrases():
    assert parse_with_rules("send me the first 3 exhibits from M12205").limit == 3
    assert parse_with_rules("send me 50 exhibits from M12205").limit == 10
    assert parse_with_rules("exhibits from M12205").limit == 10


def test_quoted_history_ignored():
    text = "Actually the transcripts please\n\nOn Mon, Sep 21 2026 someone wrote:\n> exhibits from M12383"
    req = parse_with_rules(text, subject="Re: M12205")
    assert req.matter == "M12205"
    assert req.doc_types == [DocType.TRANSCRIPTS]


def test_strip_quoted_reply_keeps_top():
    assert strip_quoted_reply("hello\n> quoted\nworld").splitlines() == ["hello", "world"]


def test_llm_used_only_when_needed():
    calls = []

    def fake_llm(subject, body):
        calls.append(body)
        return {"matter": "M12205", "doc_types": ["Transcripts"]}

    req = parse_request("M12205 exhibits", llm=fake_llm)
    assert calls == []
    assert req.doc_types == [DocType.EXHIBITS]

    req = parse_request("What was said at the M12205 hearing? I'd like to read it.", llm=fake_llm)
    assert calls
    assert req.status == ParseStatus.OK
    assert req.doc_types == [DocType.TRANSCRIPTS]
    assert req.source == "rules+llm"


def test_llm_cannot_invent_matter():
    def fake_llm(subject, body):
        return {"matter": "M99999", "doc_types": ["Exhibits"]}

    req = parse_request("exhibits please", llm=fake_llm)
    assert req.status == ParseStatus.NO_MATTER
    assert req.matter is None


def test_llm_failure_is_swallowed():
    def broken(subject, body):
        raise RuntimeError("down")

    req = parse_request("hello", llm=broken)
    assert req.status == ParseStatus.NO_MATTER
    assert any("llm unavailable" in n for n in req.notes)
