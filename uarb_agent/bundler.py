"""Zip downloaded files, splitting into parts when an email transport has a size ceiling."""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .models import DownloadedFile

_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name: str, fallback: str = "file") -> str:
    name = _UNSAFE.sub("_", name).strip(" .")
    return name or fallback


def archive_member_name(f: DownloadedFile) -> str:
    """<docid> - <title>.<ext>, so the ZIP reads well without opening anything."""
    ext = Path(f.served_name).suffix or f.row.extension or ""
    title = safe_name(f.row.title, "")[:80]
    stem = safe_name(f.row.doc_id, "doc")
    return f"{stem} - {title}{ext}" if title else f"{stem}{ext}"


@dataclass
class ZipPart:
    path: Path
    members: list[DownloadedFile] = field(default_factory=list)
    size: int = 0


@dataclass
class Bundle:
    parts: list[ZipPart]
    too_large: list[DownloadedFile] = field(default_factory=list)

    @property
    def files(self) -> list[DownloadedFile]:
        return [m for p in self.parts for m in p.members]


def _zip(files: list[DownloadedFile], dest: Path) -> int:
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as z:
        used: set[str] = set()
        for f in files:
            name = archive_member_name(f)
            if name in used:
                name = f"{Path(name).stem} ({f.row.doc_id}){Path(name).suffix}"
            used.add(name)
            z.write(f.path, arcname=name)
    return dest.stat().st_size


def bundle(files: list[DownloadedFile], out_dir: Path, stem: str, budget_bytes: int | None = None) -> Bundle:
    """Pack files into one ZIP, or several parts if the budget forces it.

    PDFs barely compress, so the packing estimate uses raw size. If a single file
    exceeds the budget on its own it is reported in ``too_large`` rather than sent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if not files:
        return Bundle(parts=[])
    if budget_bytes is None:
        dest = out_dir / f"{stem}.zip"
        return Bundle(parts=[ZipPart(path=dest, members=list(files), size=_zip(files, dest))])

    groups: list[list[DownloadedFile]] = []
    too_large: list[DownloadedFile] = []
    current: list[DownloadedFile] = []
    current_size = 0
    for f in files:
        if f.size > budget_bytes:
            too_large.append(f)
            continue
        if current and current_size + f.size > budget_bytes:
            groups.append(current)
            current, current_size = [], 0
        current.append(f)
        current_size += f.size
    if current:
        groups.append(current)

    parts: list[ZipPart] = []
    for i, group in enumerate(groups, 1):
        name = f"{stem}.zip" if len(groups) == 1 else f"{stem} part {i} of {len(groups)}.zip"
        dest = out_dir / name
        size = _zip(group, dest)
        if size > budget_bytes and len(group) > 1:
            # compression estimate was off; fall back to one file per part for this group
            dest.unlink()
            for j, f in enumerate(group, 1):
                d = out_dir / f"{stem} part {i}.{j}.zip"
                parts.append(ZipPart(path=d, members=[f], size=_zip([f], d)))
            continue
        parts.append(ZipPart(path=dest, members=group, size=size))
    return Bundle(parts=parts, too_large=too_large)
