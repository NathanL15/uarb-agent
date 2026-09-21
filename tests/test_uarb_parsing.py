from datetime import date

from uarb_agent.uarb import classify_row, parse_date


def cells(*items):
    return [{"t": t, "x": x} for t, x in items]


def test_other_documents_row():
    row = classify_row(cells(("102674", 6), ("Board Order", 122), ("07/08/2026", 1486), ("Public", 6), ("Preview", 1515), ("GO GET IT", 1411), (".pdf", 1383)))
    assert row.doc_id == "102674"
    assert row.title == "Board Order"
    assert row.filed == date(2026, 7, 8)
    assert row.security == "Public"
    assert row.extension == ".pdf"


def test_exhibit_row_uses_exhibit_code():
    row = classify_row(cells(("Application", 115), ("04/07/2025", 1470), ("Public", 6), ("H-1", 6), ("Preview", 1512), ("GO GET IT", 1408), (".pdf", 1386)))
    assert row.doc_id == "H-1"
    assert row.title == "Application"


def test_numeric_title_is_not_mistaken_for_id():
    row = classify_row(cells(("2025", 122), ("102674", 6), ("07/08/2026", 1486)))
    assert row.doc_id == "102674"
    assert row.title == "2025"


def test_row_without_id_is_dropped():
    assert classify_row(cells(("Loading", 100))) is None


def test_title_fragments_joined():
    row = classify_row(cells(("99001", 6), ("Part one", 122), ("continued", 122), ("01/02/2024", 1486)))
    assert row.title == "Part one continued"


def test_parse_date():
    assert parse_date("04/07/2025") == date(2025, 4, 7)
    assert parse_date("Decision Date") is None
    assert parse_date("13/40/2025") is None


def test_exhibit_id_with_parentheses():
    row = classify_row(cells(("Letters of Comment - PART 2", 115), ("02/01/2022", 1470), ("Public", 6), ("N-14-(i)", 6), ("Preview", 1512), ("GO GET IT", 1408), (".pdf", 1386)))
    assert row.doc_id == "N-14-(i)"


def test_transcript_row_derives_id():
    row = classify_row(cells(("09/12/2022", 6), ("September 12, 2022", 115), ("Evening Session", 409), ("Pdf", 703), ("Public", 6), ("Preview", 1509), ("GO GET IT", 1405)))
    assert row.doc_id == "09/12/2022 September 12, 2022 Evening Session"
    assert row.title == "September 12, 2022 Evening Session"
    assert row.extension == ".pdf"
    assert row.filed == date(2022, 9, 12)


def test_recording_row_without_security_or_extension():
    row = classify_row(cells(("09/12/2022", 6), ("M10431 - NS Power 2022 GRA - Monday", 115), ("Preview", 1512), ("GO GET IT", 1408)))
    assert row.doc_id == "09/12/2022 M10431 - NS Power 2022 GRA - Monday"
    assert row.extension == ""
