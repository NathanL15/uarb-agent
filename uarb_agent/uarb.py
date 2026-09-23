"""Playwright client for the UARB Public Documents Database (FileMaker WebDirect).

The site has no API (XML publishing and the Data API are both disabled), and it
renders a Vaadin UI whose element ids change with the viewport, so this module
reads the page the way a person does: by labels, by the shape of the values and by
the grid rows that are actually visible. The viewport is pinned so the layout is
stable between runs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import unquote
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Playwright, TimeoutError as PWTimeout, async_playwright

from .models import DocType, DocumentRow, DownloadedFile, FetchResult, MatterMetadata, MatterNotFound

log = logging.getLogger(__name__)

VIEWPORT = {"width": 1600, "height": 1200}
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
DOC_NO_RE = re.compile(r"^\d{3,8}$")
EXHIBIT_ID_RE = re.compile(r"^[A-Z]{1,4}-\d+[A-Za-z0-9.()\[\]-]*$")
EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")
EXT_WORD_RE = re.compile(r"^(pdf|docx?|xlsx?|pptx?|csv|txt|zip|mp3|mp4|m4a|wav|wma|htm|html)$", re.I)
SECURITY_RE = re.compile(r"^(Public|Confidential|Restricted|Private|Redacted)$", re.I)
TAB_RE = re.compile(r"^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings) - (\d+)$")

HEADER_LABELS = ["Matter No", "Status", "Title - Description", "Type", "Category", "Date Received", "Decision Date", "Date Final Submissions", "Outcome"]

# Label -> value assignment by geometry. Labels sit above their values and are
# left-aligned with them (within a few px). Stacked labels ("Matter No" over
# "Status") map to stacked values in the same order.
HEADER_JS = r"""(labels) => {
  const vis = e => !!e.offsetParent;
  const all = [...document.querySelectorAll('body *')].filter(vis);
  const tabEl = all.find(e => e.children.length === 0 && /^Exhibits - \d+/.test(e.textContent.trim()));
  const tabTop = tabEl ? tabEl.getBoundingClientRect().top : 400;
  const found = [];
  for (const e of document.querySelectorAll('.fm-text-paragraph')) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.top > tabTop || r.width === 0) continue;
    const parts = e.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    if (!parts.length || !parts.every(p => labels.includes(p))) continue;
    const lineH = r.height / parts.length;
    parts.forEach((p, i) => found.push({label: p, x: r.left, y: r.top + lineH * i}));
  }
  const values = [];
  for (const e of document.querySelectorAll('.iwps_edit_box')) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.top > tabTop || r.width === 0) continue;
    values.push({text: e.innerText.trim(), x: r.left, y: r.top, w: r.width});
  }
  // The title is a text paragraph rather than an edit box.
  for (const e of document.querySelectorAll('.fm-text-paragraph')) {
    if (!vis(e)) continue;
    const t = e.innerText.trim();
    if (!t || labels.includes(t) || /^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings|Hearings|Related Matters|Search|More Search Options|Tribunal Home|Back to Search Results|Public Documents Database)/.test(t)) continue;
    if (t.split('\n').every(p => labels.includes(p.trim()))) continue;
    const r = e.getBoundingClientRect();
    if (r.top > tabTop || r.width === 0) continue;
    values.push({text: t, x: r.left, y: r.top, w: r.width});
  }
  const out = {};
  const used = new Set();
  found.sort((a, b) => a.y - b.y || a.x - b.x);
  for (const l of found) {
    let best = null, bd = 1e9;
    values.forEach((v, i) => {
      if (used.has(i)) return;
      const dx = Math.abs(v.x - l.x), dy = v.y - l.y;
      if (dx > 24 || dy < 0 || dy > 200) return;
      const d = dy + dx * 4;
      if (d < bd) { bd = d; best = i; }
    });
    if (best !== null) { used.add(best); out[l.label] = values[best].text; } else out[l.label] = '';
  }
  return out;
}"""

COUNTS_JS = r"""() => {
  const out = {};
  for (const e of document.querySelectorAll('body *')) {
    if (e.children.length || !e.offsetParent) continue;
    for (const line of e.innerText.split('\n')) {
      const m = line.trim().match(/^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings) - (\d+)$/);
      if (m) out[m[1]] = parseInt(m[2], 10);
    }
  }
  return out;
}"""

# Every visible leaf text in each rendered grid row, with its x position so the
# caller can classify cells by shape and column. `index` is the row's absolute
# position in the grid (pixel offset / row height), which survives scrolling.
ROWS_JS = r"""() => {
  const trs = [...document.querySelectorAll('.v-grid-row')];
  return trs.map(tr => {
    const cells = [];
    for (const e of tr.querySelectorAll('*')) {
      if (e.children.length || !e.offsetParent) continue;
      const t = e.innerText.trim();
      if (!t) continue;
      const r = e.getBoundingClientRect();
      cells.push({t, x: Math.round(r.left)});
    }
    // Vaadin's escalator positions each row with translate3d(0, <absolute px>, 0)
    const m = (tr.style.transform || '').match(/translate3d\(\s*[-\d.]+px,\s*([-\d.]+)px/);
    const y = m ? parseFloat(m[1]) : tr.getBoundingClientRect().top;
    const h = tr.getBoundingClientRect().height || 68;
    return {index: Math.round(y / h), cells};
  });
}"""

SCROLL_JS = r"""(frac) => {
  const s = document.querySelector('.v-grid-scroller-vertical');
  if (!s) return {done: true, top: 0, max: 0};
  const before = s.scrollTop;
  s.scrollTop = before + s.clientHeight * frac;
  return {done: s.scrollTop === before, top: s.scrollTop, max: s.scrollHeight - s.clientHeight};
}"""


def parse_date(text: str) -> Optional[date]:
    text = (text or "").strip()
    if not DATE_RE.match(text):
        return None
    try:
        return datetime.strptime(text, "%m/%d/%Y").date()
    except ValueError:
        return None


def classify_row(cells: list[dict], ordinal: int | None = None) -> Optional[DocumentRow]:
    """Turn the leaf texts of one grid row into a DocumentRow, by value shape.

    Exhibits/Key/Other rows carry an id in the first column (102674, H-1, N-14-(i)).
    Transcript and recording rows have no id at all, only a date and a title, so
    one is derived from those.
    """
    doc_id = title = filed = security = ext = None
    leftovers: list[str] = []
    for c in cells:
        t = c["t"]
        if t.upper() in ("GO GET IT", "PREVIEW"):
            continue
        if DATE_RE.match(t) and filed is None:
            filed = t
        elif EXT_RE.match(t) and ext is None:
            ext = t
        elif EXT_WORD_RE.match(t) and ext is None:
            ext = "." + t.lower()
        elif SECURITY_RE.match(t) and security is None:
            security = t
        elif (DOC_NO_RE.match(t) or EXHIBIT_ID_RE.match(t)) and doc_id is None and c["x"] < 120:
            doc_id = t
        elif title is None:
            title = t
        else:
            leftovers.append(t)
    if leftovers and title:
        title = " ".join([title] + leftovers)
    if not doc_id:
        if not (filed and title):
            return None
        doc_id = f"{filed} {title}".strip()
        if ordinal is not None:
            doc_id = f"{ordinal:03d} {doc_id}"
    return DocumentRow(doc_id=doc_id, title=title or "", filed=parse_date(filed or ""), security=security or "", extension=ext or "")


def row_is_loaded(cells: list[dict]) -> bool:
    return any(c["t"].upper() == "GO GET IT" for c in cells)



class FileTooLarge(Exception):
    def __init__(self, name: str, size: int):
        super().__init__(f"{name} is {size} bytes")
        self.name = name
        self.size = size


class UarbClient:
    def __init__(self, url: str, download_dir: Path, headless: bool = True, nav_timeout_s: int = 60, download_timeout_s: int = 180):
        self.url = url
        self.download_dir = Path(download_dir)
        self.headless = headless
        self.nav_timeout = nav_timeout_s * 1000
        self.download_timeout = download_timeout_s * 1000
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    async def __aenter__(self) -> "UarbClient":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def new_page(self) -> tuple[BrowserContext, Page]:
        assert self._browser is not None
        ctx = await self._browser.new_context(accept_downloads=True, viewport=VIEWPORT)
        page = await ctx.new_page()
        page.set_default_timeout(self.nav_timeout)
        return ctx, page

    # -- navigation ---------------------------------------------------------

    async def open_matter(self, page: Page, matter: str, attempts: int = 3) -> None:
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self._open_matter_once(page, matter)
                return
            except MatterNotFound:
                raise
            except (PWTimeout, AssertionError) as exc:
                last = exc
                log.warning("open_matter %s attempt %d failed: %s", matter, attempt, str(exc).splitlines()[0])
                await page.wait_for_timeout(1500 * attempt)
        raise RuntimeError(f"could not open {matter} after {attempts} attempts: {last}")

    async def _open_matter_once(self, page: Page, matter: str) -> None:
        await page.goto(self.url, wait_until="domcontentloaded")
        placeholder = page.get_by_text("eg M01234", exact=True)
        await placeholder.wait_for()
        # WebDirect shows an "Action is Running" window while the layout loads.
        await page.locator(".v-window").wait_for(state="hidden", timeout=30000)
        await page.wait_for_timeout(300)
        field = placeholder.locator("xpath=..").locator("div.text").first
        typed = ""
        for _ in range(4):
            await field.click()
            await page.wait_for_function("() => document.activeElement && document.activeElement.isContentEditable", timeout=10000)
            await page.wait_for_timeout(250)
            await page.keyboard.type(matter)
            await page.wait_for_timeout(150)
            typed = await page.evaluate("() => document.activeElement.innerText.trim()")
            if typed == matter:
                break
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
        assert typed == matter, f"could not type matter number (got {typed!r})"
        await page.keyboard.press("Enter")
        deadline = time.monotonic() + self.nav_timeout / 1000
        while time.monotonic() < deadline:
            if await page.get_by_text(re.compile(r"^Exhibits - \d+$")).count():
                await page.wait_for_timeout(400)
                return
            windows = await page.evaluate("() => [...document.querySelectorAll('.v-window')].map(w => w.innerText)")
            if any("No Records Found" in w for w in windows):
                await page.locator(".v-window").get_by_role("button", name="OK").click()
                raise MatterNotFound(matter)
            await page.wait_for_timeout(300)
        raise PWTimeout(f"matter page for {matter} did not appear")

    # -- reading --------------------------------------------------------------

    async def read_metadata(self, page: Page, matter: str) -> MatterMetadata:
        fields: dict[str, str] = await page.evaluate(HEADER_JS, HEADER_LABELS)
        counts_raw: dict[str, int] = await page.evaluate(COUNTS_JS)
        decision_label = "Decision Date" if fields.get("Decision Date") is not None else ("Date Final Submissions" if fields.get("Date Final Submissions") is not None else "")
        decision_text = fields.get("Decision Date") or fields.get("Date Final Submissions") or ""
        counts = {dt: int(counts_raw.get(dt.value, 0)) for dt in DocType}
        meta = MatterMetadata(
            matter=fields.get("Matter No") or matter,
            title=fields.get("Title - Description", ""),
            status=fields.get("Status", ""),
            type=fields.get("Type", ""),
            category=fields.get("Category", ""),
            date_received=parse_date(fields.get("Date Received", "")),
            decision_date=parse_date(decision_text),
            decision_date_label=decision_label if parse_date(decision_text) else "",
            outcome=fields.get("Outcome", ""),
            counts=counts,
        )
        if meta.matter.upper() != matter.upper():
            log.warning("page shows %s but %s was requested", meta.matter, matter)
        return meta

    async def open_tab(self, page: Page, doc_type: DocType) -> int:
        tab = page.get_by_text(re.compile(rf"^{re.escape(doc_type.value)} - (\d+)$"))
        label = await tab.first.inner_text()
        count = int(TAB_RE.match(label.strip()).group(2))
        if count == 0:
            return 0
        await tab.first.click()
        await page.wait_for_selector(".v-grid-row", timeout=self.nav_timeout)
        await page.wait_for_timeout(600)
        return count

    async def list_documents(self, page: Page, expected: int | None = None) -> list[DocumentRow]:
        """Scroll the virtual grid and collect every row once, in display order.

        Vaadin renders placeholder rows before their data arrives, so each step
        waits until every visible row has its buttons before reading it. Rows are
        keyed by their absolute grid index, so two identical-looking rows stay two.
        """
        by_index: dict[int, DocumentRow] = {}
        for step, pause in ((0.7, 250), (0.4, 600)):
            await page.evaluate("() => { const s = document.querySelector('.v-grid-scroller-vertical'); if (s) s.scrollTop = 0; }")
            await page.wait_for_timeout(pause)
            stalls = 0
            for _ in range(600):
                for item in await self._loaded_rows(page):
                    if item["index"] in by_index:
                        continue
                    row = classify_row(item["cells"])
                    if row is not None:
                        row.grid_index = item["index"]
                        by_index[item["index"]] = row
                if expected is not None and len(by_index) >= expected:
                    break
                state = await page.evaluate(SCROLL_JS, step)
                await page.wait_for_timeout(pause)
                if state["done"]:
                    stalls += 1
                    if stalls >= 2:
                        break
            if expected is None or len(by_index) >= expected:
                break
            log.info("grid pass collected %d of %d rows; doing a slower pass", len(by_index), expected)
        await page.evaluate("() => { const s = document.querySelector('.v-grid-scroller-vertical'); if (s) s.scrollTop = 0; }")
        await page.wait_for_timeout(300)
        rows = [by_index[i] for i in sorted(by_index)]
        seen: dict[str, int] = {}
        for r in rows:
            if r.doc_id in seen:
                seen[r.doc_id] += 1
                r.doc_id = f"{r.doc_id} ({seen[r.doc_id]})"
            else:
                seen[r.doc_id] = 1
        return rows

    async def _loaded_rows(self, page: Page) -> list[dict]:
        deadline = time.monotonic() + 4.0
        raw = await page.evaluate(ROWS_JS)
        while time.monotonic() < deadline:
            raw = await page.evaluate(ROWS_JS)
            if raw and all(row_is_loaded(r["cells"]) for r in raw if r["cells"]):
                break
            await page.wait_for_timeout(150)
        return [r for r in raw if r["cells"] and row_is_loaded(r["cells"])]

    # -- downloading ------------------------------------------------------------

    async def _bring_row_into_view(self, page: Page, row: DocumentRow) -> int:
        """Scroll so the row is rendered; return its position among rendered rows."""
        target = row.grid_index
        if target is None:
            raise RuntimeError(f"row {row.doc_id} has no grid index")
        for _ in range(40):
            rendered = await self._loaded_rows(page)
            for pos, item in enumerate(rendered):
                if item["index"] == target:
                    return pos
            indices = [r["index"] for r in rendered] or [0]
            direction = 1 if target > max(indices) else -1
            await page.evaluate("([d]) => { const s = document.querySelector('.v-grid-scroller-vertical'); s.scrollTop += d * s.clientHeight * 0.6; }", [direction])
            await page.wait_for_timeout(250)
        raise RuntimeError(f"row {row.doc_id} (index {target}) could not be brought into view")

    async def download_row(self, page: Page, row: DocumentRow, dest_dir: Path, remaining_bytes: int | None = None) -> list[DownloadedFile]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        pos = await self._bring_row_into_view(page, row)
        tr = page.locator(".v-grid-row").nth(pos)
        shown = (await tr.inner_text()).replace(chr(10), " ")
        if row.title and row.title.split(" ")[0] not in shown:
            raise RuntimeError(f"row at index {row.grid_index} shows {shown[:80]!r}, expected {row.title[:40]!r}")
        button = tr.get_by_text(re.compile(r"^GO GET IT$", re.I)).filter(visible=True).first
        await button.click()
        dialog = await self._download_dialog(page)
        file_buttons = dialog.locator(".fm-download-button")
        names = [n.strip() for n in await file_buttons.all_inner_texts()]
        out: list[DownloadedFile] = []
        try:
            for i, served in enumerate(names):
                url = await self._start_download(page, file_buttons.nth(i))
                target = dest_dir / _unique(dest_dir, served or f"{row.doc_id}")
                size = await self._stream(page, url, target, remaining_bytes, served or target.name)
                if size == 0:
                    target.unlink(missing_ok=True)
                    raise RuntimeError(f"{served} downloaded as an empty file")
                out.append(DownloadedFile(row=row, path=target, size=size, served_name=served or target.name))
        finally:
            close = dialog.get_by_role("button", name="Close")
            if await close.count():
                await close.first.click()
                try:
                    await dialog.wait_for(state="hidden", timeout=10000)
                except PWTimeout:
                    pass
        return out

    async def _start_download(self, page: Page, button) -> str:
        """Click a file button and return the connector URL it targets.

        Chromium sometimes treats the click as a navigation and waits for the
        server before raising a download event; the request itself is visible at
        once, so the URL is taken from there and the browser's own download is
        cancelled in favour of a streamed fetch with a size cap.
        """
        loop = asyncio.get_running_loop()
        got: asyncio.Future = loop.create_future()

        def on_request(req):
            if "/dl/" in req.url and not got.done():
                got.set_result(req.url)

        def on_download(dl):
            if not got.done():
                got.set_result(dl.url)
            asyncio.ensure_future(dl.cancel())

        page.on("request", on_request)
        page.on("download", on_download)
        try:
            await button.click()
            return await asyncio.wait_for(got, timeout=30)
        finally:
            page.remove_listener("request", on_request)
            page.remove_listener("download", on_download)

    async def _stream(self, page: Page, url: str, target: Path, cap: int | None, name: str) -> int:
        """Fetch the connector URL with the session cookies, enforcing the byte cap
        while the body streams. Connection trouble is retried, and as a last resort
        the browser's own network stack (which already holds a connection to the
        server) fetches the file."""
        cookies = {c["name"]: c["value"] for c in await page.context.cookies()}
        timeout = httpx.Timeout(self.download_timeout / 1000, connect=30)
        last: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(cookies=cookies, timeout=timeout) as client:
                    async with client.stream("GET", url) as r:
                        if r.status_code != 200:
                            raise RuntimeError(f"{name} returned HTTP {r.status_code}")
                        written = 0
                        with open(target, "wb") as fh:
                            async for chunk in r.aiter_bytes(256 * 1024):
                                written += len(chunk)
                                if cap is not None and written > cap:
                                    fh.close()
                                    target.unlink(missing_ok=True)
                                    raise FileTooLarge(name, written)
                                fh.write(chunk)
                        return written
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last = exc
                log.warning("stream attempt %d for %s failed: %s", attempt + 1, name, type(exc).__name__)
                await asyncio.sleep(2 * (attempt + 1))
        log.info("falling back to the browser's request context for %s", name)
        try:
            r = await page.request.get(url, timeout=self.download_timeout)
        except Exception as exc:
            raise RuntimeError(f"{name}: {type(last).__name__ if last else 'download'} then {type(exc).__name__}") from exc
        if r.status != 200:
            raise RuntimeError(f"{name} returned HTTP {r.status}")
        body = await r.body()
        if cap is not None and len(body) > cap:
            raise FileTooLarge(name, len(body))
        target.write_bytes(body)
        return len(body)

    async def _download_dialog(self, page: Page):
        """GO GET IT opens "Download Files" directly for documents. For transcripts
        and recordings FileMaker first asks for a filename ("Export Field to File",
        prefilled); accepting it leads to the same "Download Files" window."""
        window = page.locator(".v-window")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            texts = await page.evaluate("() => [...document.querySelectorAll('.v-window')].map(w => w.innerText)")
            if any("Download Files" in t for t in texts):
                return page.locator(".v-window", has_text="Download Files")
            if any("Export Field to File" in t for t in texts):
                await window.filter(has_text="Export Field to File").get_by_role("button", name="OK").click()
                await page.wait_for_timeout(300)
                continue
            await page.wait_for_timeout(200)
        raise PWTimeout("no download window appeared after GO GET IT")

    async def fetch(self, matter: str, doc_type: DocType, limit: int = 10, max_total_bytes: int | None = None, max_file_bytes: int | None = None, on_progress=None) -> FetchResult:
        started = datetime.now(timezone.utc)
        ctx, page = await self.new_page()
        try:
            await self.open_matter(page, matter)
            meta = await self.read_metadata(page, matter)
            _progress(on_progress, f"{matter}: {meta.title or 'untitled'}; counts {meta.counts}")
            count = await self.open_tab(page, doc_type)
            listed: list[DocumentRow] = []
            if count:
                listed = await self.list_documents(page, expected=count)
                if len(listed) != count:
                    log.warning("%s %s: tab says %d, grid yielded %d rows", matter, doc_type.value, count, len(listed))
            downloaded: list[DownloadedFile] = []
            skipped: list[str] = []
            total = 0
            for row in listed[:limit]:
                remaining = None if max_total_bytes is None else max_total_bytes - total
                if remaining is not None and remaining <= 0:
                    skipped.append(f"{row.doc_id} skipped: the {max_total_bytes // 1_048_576} MB download budget for this request is used up")
                    continue
                cap = min(x for x in (remaining, max_file_bytes) if x is not None) if (remaining is not None or max_file_bytes is not None) else None
                try:
                    files = await self.download_row(page, row, self.download_dir / matter / doc_type.value.replace(" ", "_"), remaining_bytes=cap)
                except FileTooLarge as exc:
                    skipped.append(f"{row.doc_id} ({exc.name}) is larger than {cap // 1_048_576} MB and cannot be emailed; download it from the UARB site")
                    await self._dismiss_windows(page)
                    continue
                except Exception as exc:
                    log.warning("download %s failed: %s", row.doc_id, exc)
                    skipped.append(f"{row.doc_id} could not be downloaded ({type(exc).__name__})")
                    await self._dismiss_windows(page)
                    continue
                downloaded.extend(files)
                total += sum(f.size for f in files)
                _progress(on_progress, f"downloaded {row.doc_id} ({sum(f.size for f in files) / 1_048_576:.1f} MB)")
            return FetchResult(metadata=meta, doc_type=doc_type, listed=listed, downloaded=downloaded, skipped=skipped, started=started, finished=datetime.now(timezone.utc))
        finally:
            await ctx.close()

    async def _dismiss_windows(self, page: Page) -> None:
        for name in ("Close", "Cancel", "OK"):
            btn = page.locator(".v-window").get_by_role("button", name=name)
            if await btn.count():
                try:
                    await btn.first.click(timeout=3000)
                except Exception:
                    pass


def _unique(folder: Path, name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name) or "file"
    candidate = folder / name
    i = 1
    while candidate.exists():
        candidate = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    return candidate.name


def _progress(cb, msg: str) -> None:
    log.info(msg)
    if cb:
        cb(msg)


async def fetch_once(url: str, download_dir: Path, matter: str, doc_type: DocType, limit: int = 10, headless: bool = True, **kw) -> FetchResult:
    """One-shot helper for scripts and the CLI."""
    async with UarbClient(url, download_dir, headless=headless) as client:
        return await client.fetch(matter, doc_type, limit=limit, **kw)
