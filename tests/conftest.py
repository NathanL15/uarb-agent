from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from uarb_agent.models import DocType, DocumentRow, DownloadedFile, FetchResult, MatterMetadata


@pytest.fixture
def meta():
    return MatterMetadata(
        matter="M12205",
        title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project - $69,275,000",
        status="Open",
        type="Water",
        category="Capital Expenditure Approvals",
        date_received=date(2025, 4, 7),
        decision_date=date(2025, 10, 23),
        decision_date_label="Decision Date",
        outcome="",
        counts={
            DocType.EXHIBITS: 13,
            DocType.KEY_DOCUMENTS: 6,
            DocType.OTHER_DOCUMENTS: 43,
            DocType.TRANSCRIPTS: 0,
            DocType.RECORDINGS: 0,
        },
    )


def make_files(tmp_path: Path, sizes: list[int], prefix="10") -> list[DownloadedFile]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i, size in enumerate(sizes):
        p = tmp_path / f"{prefix}{i:04d}.pdf"
        p.write_bytes(b"%PDF-1.4\n" + bytes(size))
        row = DocumentRow(doc_id=f"{prefix}{i:04d}", title=f"Document {i}: draft/final?", filed=date(2026, 1, i + 1), extension=".pdf")
        out.append(DownloadedFile(row=row, path=p, size=p.stat().st_size, served_name=p.name))
    return out


@pytest.fixture
def result_factory(meta, tmp_path):
    def make(doc_type=DocType.OTHER_DOCUMENTS, n=3, sizes=None):
        files = make_files(tmp_path, sizes or [1000] * n)
        listed = [f.row for f in files]
        return FetchResult(
            metadata=meta,
            doc_type=doc_type,
            listed=listed,
            downloaded=files,
            started=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
            finished=datetime(2026, 9, 21, 12, 0, 42, tzinfo=timezone.utc),
        )

    return make
