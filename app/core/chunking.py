"""Section-aware chunking. Chunks never cross a heading boundary, keep tables
intact where possible, and carry the breadcrumb so retrieval has context."""
from __future__ import annotations

from app.core.models import Chunk
from app.core.parsing import Section


def chunk_sections(
    sections: list[Section], doc_meta: dict, max_chars: int = 1200, overlap_chars: int = 150
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for sec in sections:
        # paragraph = run of consecutive non-blank lines (a table stays together)
        paras: list[tuple[int, str]] = []
        buf: list[str] = []
        first_page = sec.page
        for page, line in sec.lines:
            if line.strip():
                if not buf:
                    first_page = page
                buf.append(line)
            elif buf:
                paras.append((first_page, "\n".join(buf)))
                buf = []
        if buf:
            paras.append((first_page, "\n".join(buf)))
        if not paras:
            continue

        header = f"[{sec.breadcrumb}]"
        cur: list[tuple[int, str]] = []
        size = 0

        def flush() -> None:
            nonlocal cur, size
            if not cur:
                return
            text = header + "\n" + "\n\n".join(t for _, t in cur)
            idx = len(chunks)
            chunks.append(
                Chunk(
                    id=f"{doc_meta['doc_id']}::{idx}",
                    text=text,
                    meta={**doc_meta, "section": sec.breadcrumb, "page": cur[0][0], "chunk_index": idx},
                )
            )
            # overlap: carry the tail paragraph forward if it is small
            tail = cur[-1]
            cur = [tail] if len(tail[1]) <= overlap_chars else []
            size = sum(len(t) for _, t in cur)

        for page, para in paras:
            # hard-split oversized paragraphs (e.g. a very long table) on line boundaries
            pieces = [para]
            if len(para) > max_chars:
                pieces, acc = [], ""
                for ln in para.splitlines():
                    if len(acc) + len(ln) + 1 > max_chars and acc:
                        pieces.append(acc)
                        acc = ""
                    acc += ln + "\n"
                if acc.strip():
                    pieces.append(acc.rstrip())
            for piece in pieces:
                if size + len(piece) > max_chars and cur:
                    flush()
                cur.append((page, piece))
                size += len(piece)
        flush()
    return chunks
