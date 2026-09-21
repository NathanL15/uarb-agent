"""Live evaluation against the real UARB site.

Every check compares the agent's output with what the site itself says, not with
numbers typed into this file, because the database changes as filings arrive.
A handful of facts that are years old (a title, a filing date) are pinned as a
sanity check on the field mapping.

    python -m evals.live_eval                 # full suite (~5 min)
    python -m evals.live_eval --only m12205   # one case
    python -m evals.live_eval --quick         # skip the large downloads
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uarb_agent.agent import Agent  # noqa: E402
from uarb_agent.bundler import bundle  # noqa: E402
from uarb_agent.config import Settings  # noqa: E402
from uarb_agent.mail import LocalTransport  # noqa: E402
from uarb_agent.models import DocType, MatterNotFound  # noqa: E402
from uarb_agent.reply import ORDER  # noqa: E402
from uarb_agent.uarb import UarbClient  # noqa: E402

HERE = Path(__file__).parent
MAGIC = {".pdf": b"%PDF-", ".zip": b"PK\x03\x04", ".docx": b"PK\x03\x04", ".xlsx": b"PK\x03\x04", ".mp3": None, ".mp4": None, ".m4a": None, ".wav": b"RIFF"}

# (id, matter, tab, limit, pinned facts, tags)
CASES = [
    ("m12205-other", "M12205", DocType.OTHER_DOCUMENTS, 10, {"title": "Windsor Street Exchange", "date_received": "2025-04-07", "type": "Water"}, {"core"}),
    ("m12205-key", "M12205", DocType.KEY_DOCUMENTS, 10, {"title": "Windsor Street Exchange"}, {"core"}),
    ("m12205-exhibits-3", "M12205", DocType.EXHIBITS, 3, {}, {"large"}),
    ("m12205-transcripts-empty", "M12205", DocType.TRANSCRIPTS, 10, {}, {"core"}),
    ("m12383-other", "M12383", DocType.OTHER_DOCUMENTS, 10, {"date_received": "2025-07-10"}, {"core"}),
    ("m00001-other", "M00001", DocType.OTHER_DOCUMENTS, 10, {"date_received": "2008-04-11", "outcome": "Discontinued"}, {"core"}),
    ("m10431-transcripts-2", "M10431", DocType.TRANSCRIPTS, 2, {"title": "2022 General Rate Application", "type": "Electricity", "outcome": "Directions given"}, {"large"}),
    ("m10431-recordings-1", "M10431", DocType.RECORDINGS, 1, {}, {"large", "audio"}),
    ("m10431-exhibits-list", "M10431", DocType.EXHIBITS, 0, {}, {"listing"}),
]


class Check:
    def __init__(self):
        self.items: list[tuple[str, bool, str]] = []

    def __call__(self, name: str, ok: bool, detail: str = ""):
        self.items.append((name, bool(ok), detail))

    @property
    def passed(self):
        return all(ok for _, ok, _ in self.items)


async def run_case(client: UarbClient, case, settings: Settings, work: Path) -> dict:
    cid, matter, tab, limit, pinned, tags = case
    chk = Check()
    t0 = time.perf_counter()
    result = await client.fetch(matter, tab, limit=limit or 1, max_total_bytes=settings.max_total_bytes)
    if limit == 0:
        result.downloaded = []
    meta = result.metadata
    n_tab = meta.counts.get(tab, 0)

    chk("matter echoes request", meta.matter == matter, meta.matter)
    chk("title present", bool(meta.title), meta.title[:60])
    chk("type present", bool(meta.type), meta.type)
    chk("category present", bool(meta.category), meta.category)
    chk("status present", bool(meta.status), meta.status)
    chk("date received parsed", meta.date_received is not None, str(meta.date_received))
    chk("all five tab counts read", len(meta.counts) == 5 and all(isinstance(v, int) for v in meta.counts.values()), str({k.value: v for k, v in meta.counts.items()}))
    for field, want in pinned.items():
        got = str(getattr(meta, field))
        chk(f"pinned {field}", want in got, got[:60])

    chk("listing count equals tab count", len(result.listed) == n_tab, f"listed {len(result.listed)} vs tab {n_tab}")
    chk("listing ids unique", len({r.doc_id for r in result.listed}) == len(result.listed))
    chk("listing rows have titles", all(r.title for r in result.listed), "")
    if n_tab and tab in (DocType.EXHIBITS, DocType.KEY_DOCUMENTS, DocType.OTHER_DOCUMENTS):
        chk("listing rows have filing dates", all(r.filed for r in result.listed), "")

    if limit:
        expected_dl = min(limit, n_tab)
        skipped_budget = [s for s in result.skipped if "budget" in s]
        chk("downloaded min(limit, count)", len(result.downloaded) + len(skipped_budget) == expected_dl or len(result.downloaded) == expected_dl, f"downloaded {len(result.downloaded)} of expected {expected_dl}; skipped {result.skipped}")
        chk("downloaded ids are the first listed", [f.row.doc_id for f in result.downloaded] == [r.doc_id for r in result.listed[: len(result.downloaded)]] or bool(result.skipped), "")
        for f in result.downloaded:
            ext = f.path.suffix.lower()
            magic = MAGIC.get(ext, b"")
            head = f.path.read_bytes()[:8]
            chk(f"{f.row.doc_id} non-empty", f.size > 0, f"{f.size} bytes")
            if magic:
                chk(f"{f.row.doc_id} looks like {ext}", head.startswith(magic), head[:5].hex())
            chk(f"{f.row.doc_id} not an html error page", not head.lstrip().lower().startswith((b"<!doc", b"<html")), "")
        z = bundle(result.downloaded, work / cid, f"{matter} {tab.value}", budget_bytes=settings.attachment_budget_bytes)
        for part in z.parts:
            with zipfile.ZipFile(part.path) as zf:
                chk(f"{part.path.name} opens and is complete", zf.testzip() is None and len(zf.namelist()) == len(part.members), f"{len(zf.namelist())} members, {part.size / 1_048_576:.1f} MB")
            chk(f"{part.path.name} under budget", part.size <= settings.attachment_budget_bytes, f"{part.size / 1_048_576:.1f} MB")
        chk("every download is in a zip or reported too large", len(z.files) + len(z.too_large) == len(result.downloaded), "")
    return {"id": cid, "matter": matter, "tab": tab.value, "seconds": round(time.perf_counter() - t0, 1), "passed": chk.passed, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in chk.items], "counts": {k.value: v for k, v in meta.counts.items()}, "listed": len(result.listed), "downloaded": len(result.downloaded), "bytes": sum(f.size for f in result.downloaded)}


async def run_not_found(client: UarbClient) -> dict:
    chk = Check()
    t0 = time.perf_counter()
    try:
        await client.fetch("M99999", DocType.EXHIBITS, limit=1)
        chk("M99999 raises MatterNotFound", False, "no exception")
    except MatterNotFound as exc:
        chk("M99999 raises MatterNotFound", True, str(exc))
    except Exception as exc:
        chk("M99999 raises MatterNotFound", False, f"{type(exc).__name__}: {exc}")
    return {"id": "m99999-not-found", "seconds": round(time.perf_counter() - t0, 1), "passed": chk.passed, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in chk.items]}


async def run_end_to_end(settings: Settings, work: Path) -> dict:
    """The brief's example, through the whole agent with the local mailbox."""
    chk = Check()
    t0 = time.perf_counter()
    settings.data_dir = work / "e2e"
    transport = LocalTransport(settings.data_dir / "mailbox", address="agent@local")
    agent = Agent(settings, transport)
    transport.drop("nathan@example.com", "", "Hi Agent, Can you give me Other Documents files from M12205? Thanks!")
    transport.drop("nathan@example.com", "", "Please send the exhibits")
    transport.drop("nathan@example.com", "", "M99999 key documents")
    n = await agent.run_once()
    chk("three requests handled", n == 3, str(n))
    sent = transport.sent()
    chk("three replies sent", len(sent) == 3, str([m["subject"] for m in sent]))
    ok_reply = next((m for m in sent if m["subject"] == "M12205 Other Documents"), None)
    chk("reply for the brief example exists", ok_reply is not None)
    if ok_reply:
        body = ok_reply["body"]
        run_json = json.loads(next(agent.runs_dir.glob("*/run.json")).read_text()) if False else None
        counts = {}
        for m in re.finditer(r"(\d+) (Exhibits?|Key Documents?|Other Documents?|Transcripts?|Recordings?)", body):
            counts[m.group(2).rstrip("s") if m.group(2) != "Other Documents" else m.group(2)] = int(m.group(1))
        chk("reply names the matter title", "Windsor Street Exchange" in body, "")
        chk("reply states counts per tab", bool(re.search(r"I found .*Exhibits.*Key Documents.*Other Documents", body)), body[:200])
        chk("reply states the total", bool(re.search(r"\d+ files in total", body)), "")
        chk("reply states downloaded X out of Y", bool(re.search(r"I downloaded (\d+) out of the (\d+) Other Documents", body)), "")
        m = re.search(r"I downloaded (\d+) out of the (\d+) Other Documents", body)
        if m:
            chk("downloaded is min(10, count)", int(m.group(1)) == min(10, int(m.group(2))), m.group(0))
        chk("one zip attached", len(ok_reply["attachments"]) == 1 and ok_reply["attachments"][0].endswith(".zip"), str(ok_reply["attachments"]))
        if ok_reply["attachments"]:
            with zipfile.ZipFile(ok_reply["attachments"][0]) as zf:
                names = zf.namelist()
                chk("zip member count matches the reply", m is not None and len(names) == int(m.group(1)), f"{len(names)} members")
                chk("zip members are listed in the reply body", all(Path(nm).stem.split(" - ")[0] in body for nm in names), "")
    clar = next((m for m in sent if "could not find a matter number" in m["body"]), None)
    chk("missing matter gets a clarification", clar is not None)
    nf = next((m for m in sent if m["subject"] == "M99999 not found"), None)
    chk("unknown matter gets a not-found reply", nf is not None)
    runs = list(agent.runs_dir.glob("*/run.json"))
    chk("every request left an audit record", len(runs) == 3, str(len(runs)))
    return {"id": "end-to-end", "seconds": round(time.perf_counter() - t0, 1), "passed": chk.passed, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in chk.items]}


async def run_repeat(client: UarbClient) -> dict:
    """Same request twice gives the same listing: the order the site shows is stable."""
    chk = Check()
    t0 = time.perf_counter()
    a = await client.fetch("M12383", DocType.KEY_DOCUMENTS, limit=1)
    b = await client.fetch("M12383", DocType.KEY_DOCUMENTS, limit=1)
    chk("listing identical across runs", [r.doc_id for r in a.listed] == [r.doc_id for r in b.listed], f"{len(a.listed)} vs {len(b.listed)}")
    chk("metadata identical across runs", a.metadata.model_dump(mode="json") == b.metadata.model_dump(mode="json"))
    chk("same first file both times", a.downloaded and b.downloaded and a.downloaded[0].size == b.downloaded[0].size, "")
    return {"id": "repeatability", "seconds": round(time.perf_counter() - t0, 1), "passed": chk.passed, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in chk.items]}


async def main_async(args) -> int:
    settings = Settings()
    work = HERE / "work" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    work.mkdir(parents=True)
    settings.data_dir = work / "data"
    results = []
    cases = [c for c in CASES if (not args.only or args.only.lower() in c[0]) and (not args.quick or "large" not in c[5])]
    async with UarbClient(settings.uarb_url, work / "downloads", headless=settings.headless, nav_timeout_s=settings.nav_timeout_s, download_timeout_s=settings.download_timeout_s) as client:
        for case in cases:
            print(f"-- {case[0]}", file=sys.stderr)
            try:
                results.append(await run_case(client, case, settings, work))
            except Exception as exc:
                results.append({"id": case[0], "passed": False, "seconds": 0, "checks": [{"name": "case ran", "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}]})
        if not args.only:
            print("-- not found", file=sys.stderr)
            results.append(await run_not_found(client))
            print("-- repeatability", file=sys.stderr)
            try:
                results.append(await run_repeat(client))
            except Exception as exc:
                results.append({"id": "repeatability", "passed": False, "seconds": 0, "checks": [{"name": "case ran", "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}]})
    if not args.only:
        print("-- end to end", file=sys.stderr)
        try:
            results.append(await run_end_to_end(Settings(), work))
        except Exception as exc:
            results.append({"id": "end-to-end", "passed": False, "seconds": 0, "checks": [{"name": "case ran", "ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}]})

    total_checks = sum(len(r["checks"]) for r in results)
    ok_checks = sum(c["ok"] for r in results for c in r["checks"])
    stamp = datetime.now(timezone.utc)
    lines = [f"# Live eval ({stamp:%Y-%m-%d %H:%M} UTC)", "", f"{sum(r['passed'] for r in results)}/{len(results)} cases passed, {ok_checks}/{total_checks} checks passed.", ""]
    lines.append("| case | pass | s | counts (E/K/O/T/R) | listed | downloaded | MB |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        c = r.get("counts")
        cs = "/".join(str(c.get(d.value, 0)) for d in ORDER) if c else ""
        lines.append(f"| {r['id']} | {'yes' if r['passed'] else 'NO'} | {r['seconds']} | {cs} | {r.get('listed', '')} | {r.get('downloaded', '')} | {r.get('bytes', 0) / 1_048_576:.1f} |")
    lines.append("")
    for r in results:
        failed = [c for c in r["checks"] if not c["ok"]]
        if failed:
            lines.append(f"## {r['id']}: failed checks")
            for c in failed:
                lines.append(f"- {c['name']}: {c['detail']}")
            lines.append("")
    lines.append("## Every check")
    for r in results:
        lines.append(f"### {r['id']}")
        for c in r["checks"]:
            lines.append(f"- [{'x' if c['ok'] else ' '}] {c['name']}{': ' + c['detail'] if c['detail'] else ''}")
        lines.append("")
    report = "\n".join(lines)
    print(report)
    out = HERE / "reports"
    out.mkdir(exist_ok=True)
    (out / f"live-{stamp:%Y%m%d-%H%M%S}.md").write_text(report, encoding="utf-8")
    (out / f"live-{stamp:%Y%m%d-%H%M%S}.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    return 0 if all(r["passed"] for r in results) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
