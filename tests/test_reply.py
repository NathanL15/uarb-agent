from uarb_agent.bundler import bundle
from uarb_agent.models import DocType, MatterRequest, ParseStatus
from uarb_agent.reply import compose_clarification, compose_not_found, compose_success, count_sentence


def test_count_sentence_matches_brief_style(meta):
    s = count_sentence(meta)
    assert s == "I found 13 Exhibits, 6 Key Documents, and 43 Other Documents and no Transcripts or Recordings, 62 files in total."


def test_count_sentence_singular(meta):
    meta.counts[DocType.KEY_DOCUMENTS] = 1
    assert "1 Key Document," in count_sentence(meta)


def test_success_body_has_every_fact(meta, result_factory, tmp_path):
    result = result_factory(n=3)
    b = bundle(result.downloaded, tmp_path / "zips", "M12205 Other Documents")
    subject, body = compose_success(MatterRequest(matter="M12205", doc_types=[DocType.OTHER_DOCUMENTS]), result, b)
    assert subject == "M12205 Other Documents"
    assert "M12205 is about the Halifax Regional Water Commission" in body
    assert "Capital Expenditure Approvals within the Water category" in body
    assert "Its status is Open." in body
    assert "initial filing on April 7, 2025" in body
    assert "a decision on October 23, 2025" in body
    assert "I downloaded 3 out of the 43 Other Documents" in body
    assert "  - 100000  Document 0: draft/final? (01/01/2026)" in body
    assert "Fetched in 42s" in body


def test_success_mentions_parts(meta, result_factory, tmp_path):
    result = result_factory(n=4, sizes=[600_000] * 4)
    b = bundle(result.downloaded, tmp_path / "zips", "M12205", budget_bytes=1_300_000)
    subject, body = compose_success(MatterRequest(matter="M12205"), result, b, part=2, total_parts=2)
    assert subject.endswith("(part 2 of 2)")
    assert "split into 2 parts" in body
    assert body.count("  - 10000") == 2


def test_oversized_note(meta, result_factory, tmp_path):
    result = result_factory(n=2, sizes=[100, 3_000_000])
    b = bundle(result.downloaded, tmp_path / "zips", "M12205", budget_bytes=1_000_000)
    _, body = compose_success(MatterRequest(matter="M12205"), result, b)
    assert "100001 (2.9 MB) is too large to email" in body


def test_empty_tab(meta, result_factory, tmp_path):
    result = result_factory(doc_type=DocType.TRANSCRIPTS, n=0)
    b = bundle([], tmp_path / "zips", "x")
    _, body = compose_success(MatterRequest(matter="M12205"), result, b)
    assert "There are no Transcripts for this matter" in body


def test_not_found():
    subject, body = compose_not_found("M99999")
    assert "M99999" in subject and "No Records Found" in body


def test_clarifications():
    _, b1 = compose_clarification(MatterRequest(status=ParseStatus.NO_MATTER), "hi")
    assert "could not find a matter number" in b1
    _, b2 = compose_clarification(MatterRequest(matter="M12205", status=ParseStatus.NO_DOC_TYPE), "hi")
    assert "Which documents from M12205" in b2
    _, b3 = compose_clarification(MatterRequest(matter="M12205", status=ParseStatus.AMBIGUOUS_MATTER, notes=["several matter numbers mentioned: M12205, M12383"]), "hi")
    assert "M12383" in b3
