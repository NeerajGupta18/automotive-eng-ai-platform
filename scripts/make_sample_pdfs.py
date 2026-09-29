"""Render the sample Markdown HLDs to real PDFs (with bordered tables) so the PDF
ingestion path can be demonstrated and tested. Usage: python scripts/make_sample_pdfs.py"""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape


def md_to_pdf(md_path: Path, pdf_path: Path) -> None:
    ss = getSampleStyleSheet()
    story, lines, i = [], md_path.read_text().splitlines(), 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not set(lines[i].strip()) <= set("|-: "):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = Table(rows, hAlign="LEFT")
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.8, colors.black),
                                   ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                                   ("FONTSIZE", (0, 0), (-1, -1), 9)]))
            story += [t, Spacer(1, 8)]
            continue
        if ln.startswith("#"):
            level = len(ln) - len(ln.lstrip("#"))
            style = ss[{1: "Title", 2: "Heading2", 3: "Heading3"}.get(level, "Heading3")]
            story.append(Paragraph(escape(ln.lstrip("# ").strip()), style))
        elif ln.strip():
            story.append(Paragraph(escape(ln), ss["BodyText"]))
        i += 1
    SimpleDocTemplate(str(pdf_path), pagesize=A4).build(story)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent / "data" / "samples"
    for md in root.glob("hld_*.md"):
        md_to_pdf(md, md.with_suffix(".pdf"))
        print("wrote", md.with_suffix(".pdf"))
