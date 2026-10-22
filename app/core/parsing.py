"""Document parsing: PDF (text + tables + optional OCR), Markdown, plain text.
Output is a list of pages; tables are rendered as markdown rows so that the
chunker and the extractors see them as structured lines."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Page:
    number: int
    text: str


def _table_to_md(rows: list[list[str | None]]) -> str:
    clean = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows if any(c for c in r)]
    if not clean:
        return ""
    lines = ["| " + " | ".join(clean[0]) + " |", "|" + "---|" * len(clean[0])]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(lines)


def _inset(rect, dx: float, dy: float):
    import pymupdf

    r = pymupdf.Rect(rect)
    return pymupdf.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy)


def parse_pdf(path: Path, ocr: bool = True) -> list[Page]:
    """Text blocks and tables are merged in reading order (top to bottom) so that
    a table stays attached to the section heading above it."""
    import pymupdf

    pages: list[Page] = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            items: list[tuple[float, float, str]] = []   # (y0, x0, text)
            table_rects = []
            try:
                for t in page.find_tables().tables:
                    rows = []
                    for row in t.rows:
                        # get_textbox per cell keeps identifiers such as If_DoorLockCmd intact
                        rows.append([page.get_textbox(_inset(c, 1, 3)).strip() if c else "" for c in row.cells])
                    md = _table_to_md(rows)
                    if md:
                        r = pymupdf.Rect(t.bbox)
                        table_rects.append(_inset(r, 0, 3))   # tolerate text touching the border
                        items.append((r.y0, r.x0, md))
            except Exception:  # table detection is best-effort
                pass
            for b in page.get_text("blocks"):
                if b[6] != 0 or not b[4].strip():      # skip image blocks / empty
                    continue
                if any(pymupdf.Rect(b[:4]).intersects(r) for r in table_rects):
                    continue
                items.append((b[1], b[0], b[4].strip()))
            items.sort(key=lambda x: (round(x[0]), x[1]))
            text = "\n\n".join(t for _, _, t in items)
            if not text.strip() and ocr:
                text = _ocr_page(page)
            pages.append(Page(i, text.strip()))
    return pages


def _ocr_page(page) -> str:
    """OCR for scanned pages. Needs Tesseract installed; degrades to '' if absent."""
    try:
        import io

        import pytesseract
        from PIL import Image

        pix = page.get_pixmap(dpi=200)
        return pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes("png")))).strip()
    except Exception:
        return ""


def parse_text(path: Path) -> list[Page]:
    # Markdown / txt: a form-feed (\f) marks a page break if present, else one page.
    raw = path.read_text(encoding="utf-8", errors="replace")
    return [Page(i, t.strip()) for i, t in enumerate(raw.split("\f"), start=1)]


def _pdf_child(path: str, conn) -> None:
    try:
        conn.send(("ok", [(p.number, p.text) for p in parse_pdf(Path(path))]))
    except Exception as e:                                   # noqa: BLE001 - reported to the parent
        conn.send(("err", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def parse_pdf_isolated(path: Path, timeout: float = 120.0) -> list[Page]:
    """Parse a PDF in a short-lived child process. PyMuPDF's memory (about 200 MB after table detection)
    is returned to the OS when the child exits, and a malformed PDF that hangs or crashes the parser
    cannot take the API process down. Used on small cloud instances (PDF_PARSE_ISOLATION=1)."""
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_pdf_child, args=(str(path), child), daemon=True)
    proc.start()
    child.close()
    try:
        if not parent.poll(timeout):
            raise ValueError(f"PDF parsing did not finish within {timeout:g} s")
        status, data = parent.recv()
    except EOFError as e:
        raise ValueError("PDF parser process crashed") from e
    finally:
        parent.close()
        proc.join(2)
        if proc.is_alive():
            proc.terminate()
            proc.join(2)
    if status != "ok":
        raise ValueError(data)
    return [Page(n, t) for n, t in data]


def parse_document(path: Path) -> list[Page]:
    import os

    ext = path.suffix.lower()
    if ext == ".pdf":
        if os.getenv("PDF_PARSE_ISOLATION", "0") == "1":
            return parse_pdf_isolated(path, float(os.getenv("PDF_PARSE_TIMEOUT_S", "120")))
        return parse_pdf(path)
    if ext in (".md", ".txt"):
        return parse_text(path)
    raise ValueError(f"Unsupported file type: {ext}")


# ---------------------------------------------------------------- sections
@dataclass
class Section:
    title: str            # heading text without numbering
    number: str           # "3.1" or ""
    level: int
    breadcrumb: str       # "3 Software Components > 3.1 SWC_X"
    page: int             # page where section starts
    lines: list[tuple[int, str]]   # (page, text)

    @property
    def body(self) -> str:
        return "\n".join(t for _, t in self.lines)


_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_NUM_HEAD = re.compile(r"^(\d+(?:\.\d+){0,4})\.?\s+([A-Za-z_][^|]{1,90})$")


def _heading(line: str) -> tuple[int, str, str] | None:
    s = line.strip()
    m = _MD_HEAD.match(s)
    if m:
        title = m.group(2).strip()
        n = re.match(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$", title)
        return (len(m.group(1)), n.group(2) if n else title, n.group(1) if n else "")
    m = _NUM_HEAD.match(s)
    if m and not s.endswith((".", ":", ",", ";")) and len(s.split()) <= 12:
        return (m.group(1).count(".") + 1, m.group(2).strip(), m.group(1))
    return None


def split_sections(pages: list[Page]) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    cur = Section("Front matter", "", 0, "Front matter", 1, [])
    for p in pages:
        for line in p.text.splitlines():
            h = _heading(line)
            if h:
                if cur.lines or cur.title != "Front matter":
                    sections.append(cur)
                level, title, num = h
                label = f"{num} {title}".strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, label))
                cur = Section(title, num, level, " > ".join(x[1] for x in stack), p.number, [])
            else:
                cur.lines.append((p.number, line.rstrip()))
    sections.append(cur)
    return [s for s in sections if s.lines or s.title != "Front matter"]
