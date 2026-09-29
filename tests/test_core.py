import numpy as np
import pytest

from app.core.chunking import chunk_sections
from app.core.embeddings import HashingEmbedder
from app.core.models import Chunk
from app.core.parsing import Page, split_sections
from app.core.vectorstore import NumpyStore


def test_sections_and_breadcrumbs():
    pages = [Page(1, "# Doc\n\n## 3 Components\n\n### 3.1 SWC_A\n\nType: App\n\n### 3.2 SWC_B\n\nType: App")]
    secs = split_sections(pages)
    crumbs = [s.breadcrumb for s in secs]
    assert "3 Components > 3.1 SWC_A" in " | ".join(crumbs).replace("Doc > ", "")
    assert any(c.endswith("3.2 SWC_B") for c in crumbs)


def test_numbered_headings_in_pdf_text():
    secs = split_sections([Page(1, "3 Software Components\nintro text here\n3.1 SWC_A\nType: X")])
    assert [s.number for s in secs] == ["3", "3.1"]


def test_chunks_never_cross_headings_and_keep_page():
    pages = [Page(1, "## A\n\n" + "alpha " * 50), Page(2, "## B\n\n" + "beta " * 50)]
    chunks = chunk_sections(split_sections(pages), {"doc_id": "d"}, max_chars=100000)
    assert len(chunks) == 2
    assert "alpha" in chunks[0].text and "beta" not in chunks[0].text
    assert chunks[1].meta["page"] == 2 and chunks[1].meta["section"] == "B"


def test_long_section_is_split_with_size_limit():
    body = "\n\n".join(f"paragraph number {i} " + "x" * 200 for i in range(20))
    chunks = chunk_sections(split_sections([Page(1, "## Big\n\n" + body)]), {"doc_id": "d"}, max_chars=800)
    assert len(chunks) > 3
    assert all(len(c.text) < 1500 for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)


def test_hash_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    a, b = e.embed(["SWC_DoorLockCtrl"]), e.embed(["SWC_DoorLockCtrl"])
    assert np.allclose(a, b) and abs(np.linalg.norm(a[0]) - 1) < 1e-5
    # camelCase / underscore splitting lets 'door lock' match 'DoorLockCtrl'
    q = e.embed_query("door lock")
    assert float(q @ a[0]) > float(q @ e.embed(["VehicleSpeedMgr"])[0])


def test_numpy_store_persists_filters_and_deletes(tmp_path):
    e = HashingEmbedder()
    texts = ["door lock control", "vehicle speed manager", "light control"]
    chunks = [Chunk(f"{d}::0", t, {"doc_id": d}) for d, t in zip("abc", texts)]
    s = NumpyStore(tmp_path, "p")
    s.add(chunks, e.embed(texts))
    assert s.query(e.embed_query("speed"), 1)[0].meta["doc_id"] == "b"
    assert {h.meta["doc_id"] for h in s.query(e.embed_query("control"), 5, {"doc_id": ["a", "c"]})} == {"a", "c"}
    assert NumpyStore(tmp_path, "p").count() == 3            # reload from disk
    s.delete({"doc_id": "b"})
    assert s.count() == 2


def test_chroma_store_matches_numpy_behaviour(tmp_path):
    pytest.importorskip("chromadb")
    from app.core.vectorstore import ChromaStore

    e = HashingEmbedder()
    texts = ["door lock control", "vehicle speed manager", "light control"]
    chunks = [Chunk(f"{d}::0", t, {"doc_id": d}) for d, t in zip("abc", texts)]
    s = ChromaStore(tmp_path, "p")
    s.add(chunks, e.embed(texts))
    assert s.query(e.embed_query("speed"), 1)[0].meta["doc_id"] == "b"
    assert {h.meta["doc_id"] for h in s.query(e.embed_query("control"), 5, {"doc_id": ["a", "c"]})} == {"a", "c"}
    s.delete({"doc_id": "b"})
    assert s.count() == 2
