from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class DocType(str, Enum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"

    @property
    def plural_label(self) -> str:
        return self.value

    @property
    def singular_label(self) -> str:
        return {
            DocType.EXHIBITS: "Exhibit",
            DocType.KEY_DOCUMENTS: "Key Document",
            DocType.OTHER_DOCUMENTS: "Other Document",
            DocType.TRANSCRIPTS: "Transcript",
            DocType.RECORDINGS: "Recording",
        }[self]


class ParseStatus(str, Enum):
    OK = "ok"
    NO_MATTER = "no_matter"
    NO_DOC_TYPE = "no_doc_type"
    AMBIGUOUS_MATTER = "ambiguous_matter"


class MatterRequest(BaseModel):
    matter: Optional[str] = None
    doc_types: list[DocType] = Field(default_factory=list)
    status: ParseStatus = ParseStatus.OK
    limit: int = 10
    source: str = "rules"
    notes: list[str] = Field(default_factory=list)


class DocumentRow(BaseModel):
    doc_id: str
    title: str = ""
    filed: Optional[date] = None
    security: str = ""
    extension: str = ""
    grid_index: Optional[int] = None

    @property
    def filed_text(self) -> str:
        return self.filed.strftime("%m/%d/%Y") if self.filed else ""


class MatterMetadata(BaseModel):
    matter: str
    title: str = ""
    status: str = ""
    type: str = ""
    category: str = ""
    date_received: Optional[date] = None
    decision_date: Optional[date] = None
    decision_date_label: str = ""
    outcome: str = ""
    counts: dict[DocType, int] = Field(default_factory=dict)

    @property
    def total_files(self) -> int:
        return sum(self.counts.values())


class DownloadedFile(BaseModel):
    row: DocumentRow
    path: Path
    size: int
    served_name: str


class FetchResult(BaseModel):
    metadata: MatterMetadata
    doc_type: DocType
    listed: list[DocumentRow]
    downloaded: list[DownloadedFile]
    skipped: list[str] = Field(default_factory=list)
    started: datetime
    finished: datetime

    @property
    def duration_s(self) -> float:
        return (self.finished - self.started).total_seconds()


class MatterNotFound(Exception):
    def __init__(self, matter: str):
        super().__init__(f"{matter} was not found in the UARB database")
        self.matter = matter
