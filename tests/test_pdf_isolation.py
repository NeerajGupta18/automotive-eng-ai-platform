"""PDF parsing in a short-lived child process (PDF_PARSE_ISOLATION=1, used on small cloud instances)."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_isolated_pdf_parsing_matches_in_process(tmp_path, monkeypatch):
    from app.core.parsing import parse_document, parse_pdf, parse_pdf_isolated
    pdf = ROOT / "data" / "samples" / "hld_bcm_v1.pdf"
    monkeypatch.setenv("PDF_PARSE_ISOLATION", "1")
    assert parse_document(pdf) == parse_pdf(pdf)
    with pytest.raises(ValueError, match="did not finish"):
        parse_pdf_isolated(pdf, timeout=0.001)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(ValueError):
        parse_pdf_isolated(broken)
