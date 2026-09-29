"""Rule-based architecture entity extraction for AUTOSAR-style HLD documents.

Why rules first: extraction of ports/interfaces must be deterministic and
auditable. An LLM fallback (llm_fallback) exists for documents that do not follow
the naming taxonomy; its output is always tagged origin='llm' and queued for review.

Recognised conventions (configurable through Taxonomy):
  * a heading whose first token starts with SWC_/CDD_/Comp_  -> software component
  * a heading whose first token starts with If_/IF_/Itf_     -> port interface
  * port tables  (columns: Port | Direction | Interface)      or 'Port: name | P-Port | If_X'
  * 'Type:', 'Runnables:', 'Data elements:', 'Operations:' key-value lines
  * signal tables (column 'Signal', optional Type/Unit/Source/Sink)"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.parsing import Page, Section, split_sections


@dataclass(frozen=True)
class Taxonomy:
    component_prefixes: tuple[str, ...] = ("SWC_", "CDD_", "Comp_", "Swc_")
    interface_prefixes: tuple[str, ...] = ("If_", "IF_", "Itf_")


def norm_dir(s: str) -> str:
    t = s.strip().lower().replace("-port", "").replace(" port", "").replace("_", "").replace("/", "")
    if t in ("p", "provided", "provide", "provider", "provides", "out", "output", "sender"):
        return "P"
    if t in ("r", "required", "require", "requirer", "requires", "in", "input", "receiver"):
        return "R"
    if t in ("pr", "providedrequired", "prport"):
        return "PR"
    return "?"


# ---------------------------------------------------------------- tables
def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_tables(lines: list[tuple[int, str]]) -> tuple[list[dict], list[tuple[int, str]]]:
    """Returns (tables, non_table_lines). A table = {'header': [...], 'rows': [[...]], 'page': n}."""
    tables, rest, i = [], [], 0
    while i < len(lines):
        page, ln = lines[i]
        nxt = lines[i + 1][1] if i + 1 < len(lines) else ""
        if ln.strip().startswith("|") and nxt.strip() and set(nxt.strip()) <= set("|-: ") and "-" in nxt:
            header = [h.lower() for h in _cells(ln)]
            rows, i = [], i + 2
            while i < len(lines) and lines[i][1].strip().startswith("|"):
                rows.append((lines[i][0], _cells(lines[i][1])))
                i += 1
            tables.append({"header": header, "rows": rows, "page": page})
            continue
        rest.append((page, ln))
        i += 1
    return tables, rest


def _col(header: list[str], *names: str) -> int | None:
    for n in names:
        for i, h in enumerate(header):
            if h == n or h.startswith(n):
                return i
    return None


def _get(row: list[str], idx: int | None) -> str:
    return row[idx].strip() if idx is not None and idx < len(row) else ""


_KV = re.compile(r"^\s*[-*]?\s*\**([A-Za-z ]{3,20}?)\**\s*:\s*(.+)$")
_INLINE_PORT = re.compile(r"^\s*[-*]?\s*Port\s*:\s*(\w+)\s*[|,]\s*([A-Za-z\-/ ]+?)\s*[|,]\s*(\w+)\s*$", re.I)
_ELEM = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*$")


def _parse_elements(text: str) -> list[dict]:
    out = []
    for part in re.split(r"[;]", text):
        m = _ELEM.match(part.strip())
        if m:
            attrs = [a.strip() for a in (m.group(2) or "").split(",")]
            out.append({"name": m.group(1), "type": attrs[0] if attrs and attrs[0] else "",
                        "unit": attrs[1] if len(attrs) > 1 else ""})
    return out


# ---------------------------------------------------------------- main
def _subtree(sections: list[Section], i: int) -> list[Section]:
    out, base = [sections[i]], sections[i].level
    for s in sections[i + 1:]:
        if s.level <= base:
            break
        out.append(s)
    return out


def extract_entities(pages: list[Page], tax: Taxonomy = Taxonomy()) -> dict:
    sections = split_sections(pages)
    components, interfaces, signals = [], [], []

    for i, sec in enumerate(sections):
        first = (re.findall(r"[A-Za-z0-9_]+", sec.title) or [""])[0]
        is_comp = first.startswith(tax.component_prefixes)
        is_if = first.startswith(tax.interface_prefixes)
        if not (is_comp or is_if):
            # signal tables may sit in any section
            _collect_signal_tables(sec.lines, sec, signals)
            continue

        lines = [ln for s in _subtree(sections, i) for ln in s.lines]
        tables, rest = parse_tables(lines)
        src = {"section": sec.breadcrumb, "page": sec.page}
        kv: dict[str, str] = {}
        last: str | None = None          # key being continued (PDF text wraps long lines)
        for _, ln in rest:
            m = _KV.match(ln)
            if m:
                key = m.group(1).strip().lower()
                if key in kv:
                    last = None
                else:
                    kv[key], last = m.group(2).strip(), key
            elif not ln.strip():
                last = None
            elif last:
                kv[last] += " " + ln.strip()

        if is_comp:
            ports = []
            for t in tables:
                h = t["header"]
                ni, ii = _col(h, "port name", "port", "name"), _col(h, "interface")
                di = _col(h, "direction", "kind", "type")
                if ni is None or ii is None:
                    continue
                for pg, row in t["rows"]:
                    if _get(row, ni):
                        ports.append({"name": _get(row, ni), "direction": norm_dir(_get(row, di)),
                                      "declared_direction": _get(row, di), "interface": _get(row, ii),
                                      "page": pg})
            for pg, ln in rest:
                m = _INLINE_PORT.match(ln)
                if m:
                    ports.append({"name": m.group(1), "direction": norm_dir(m.group(2)),
                                  "declared_direction": m.group(2), "interface": m.group(3), "page": pg})
            runnables = [r.strip() for r in re.split(r"[,;]", kv.get("runnables", kv.get("runnable", ""))) if r.strip()]
            components.append({"name": first, "type": kv.get("type", ""), "description": kv.get("description", ""),
                               "ports": ports, "runnables": runnables, "source": src, "origin": "rules"})
        else:
            elements = _parse_elements(kv.get("data elements", kv.get("data element", kv.get("elements", ""))))
            for t in tables:
                h = t["header"]
                ni = _col(h, "data element", "element", "signal", "name")
                if ni is None:
                    continue
                ti, ui = _col(h, "type", "datatype", "data type"), _col(h, "unit")
                for _, row in t["rows"]:
                    if _get(row, ni):
                        elements.append({"name": _get(row, ni), "type": _get(row, ti), "unit": _get(row, ui)})
            operations = [o.strip() for o in kv.get("operations", "").split(";") if o.strip()]
            interfaces.append({"name": first, "type": kv.get("type", ""), "elements": elements,
                               "operations": operations, "source": src, "origin": "rules"})
            for e in elements:
                signals.append({**e, "interface": first, "source_comp": "", "sink_comp": "", "source": src})

    return {"components": components, "interfaces": interfaces, "signals": signals}


def _collect_signal_tables(lines, sec: Section, signals: list[dict]) -> None:
    tables, _ = parse_tables(lines)
    for t in tables:
        h = t["header"]
        si = _col(h, "signal")
        if si is None or _col(h, "port") is not None:
            continue
        ti, ui = _col(h, "type", "datatype", "data type"), _col(h, "unit")
        so, sk = _col(h, "source", "sender", "from"), _col(h, "sink", "receiver", "to")
        for _, row in t["rows"]:
            if _get(row, si):
                signals.append({"name": _get(row, si), "type": _get(row, ti), "unit": _get(row, ui),
                                "interface": "", "source_comp": _get(row, so), "sink_comp": _get(row, sk),
                                "source": {"section": sec.breadcrumb, "page": t["page"]}})


# ---------------------------------------------------------------- LLM fallback
LLM_EXTRACT_PROMPT = """Extract AUTOSAR architecture entities from the text below.
Return ONLY JSON: {{"components":[{{"name":"","ports":[{{"name":"","direction":"P|R","interface":""}}]}}],
"interfaces":[{{"name":"","type":"","elements":[{{"name":"","type":"","unit":""}}]}}]}}
Do not invent anything not stated in the text. Text:
<<<
{text}
>>>"""


def llm_fallback(llm, sections: list[Section], max_sections: int = 60) -> dict:
    """Used only when the taxonomy finds nothing. Output is tagged origin='llm' and must be reviewed."""
    comps, ifs = [], []
    for sec in [s for s in sections if len(s.body) > 200][:max_sections]:
        raw = llm.generate("You extract structured data. Output JSON only.", LLM_EXTRACT_PROMPT.format(text=sec.body[:3500]))
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        src = {"section": sec.breadcrumb, "page": sec.page}
        for c in data.get("components", []):
            if c.get("name"):
                c["ports"] = [{**p, "direction": norm_dir(str(p.get("direction", ""))), "declared_direction": p.get("direction", "")}
                              for p in c.get("ports", []) if p.get("name")]
                comps.append({"type": "", "description": "", "runnables": [], **c, "source": src, "origin": "llm"})
        for i in data.get("interfaces", []):
            if i.get("name"):
                ifs.append({"type": "", "operations": [], "elements": [], **i, "source": src, "origin": "llm"})
    return {"components": comps, "interfaces": ifs, "signals": []}
