"""ReqIF 1.2 (OMG Requirements Interchange Format) export and import.

ReqIF is how requirements travel between OEMs and suppliers (IBM DOORS, Polarion, codebeamer, Jama, PTC RV&S).
  export   safety goals and requirements (HARA), cybersecurity goals and requirements (TARA) and ADAS
           requirements, with their trace links as SPEC-RELATIONs and one SPECIFICATION per discipline.
           IDENTIFIERs are stable (derived from project and reference), so a partner tool recognises updates.
  parse    a ReqIF file or .reqifz archive from a partner: spec objects with every attribute (string,
           XHTML, enumeration, integer, real, boolean, date), in document order. Untrusted input: DOCTYPE/ENTITY
           declarations are rejected, sizes are limited, archives are read in memory only."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

NS = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"
XHTML = "http://www.w3.org/1999/xhtml"
MAX_BYTES = 20 * 1024 * 1024
MAX_OBJECTS = 20000
ID_NS = uuid.UUID("5b3c7a52-2f1e-4b8e-9a51-6d1c2a0f7e11")

EXPORT_KINDS = [("hara", "goal", "Safety goals (ISO 26262)"), ("hara", "fsr", "Functional safety requirements"),
                ("hara", "tsr", "Technical safety requirements"), ("tara", "cgoal", "Cybersecurity goals (ISO/SAE 21434)"),
                ("tara", "csr", "Cybersecurity requirements"), ("adas", "requirement", "ADAS (AEB) requirements")]
ATTRS = [("AD-FOREIGN-ID", "ReqIF.ForeignID"), ("AD-NAME", "ReqIF.Name"), ("AD-TEXT", "ReqIF.Text"),
         ("AD-MODULE", "Module"), ("AD-KIND", "Kind"), ("AD-STATUS", "Status"), ("AD-CLASS", "Classification"),
         ("AD-ALLOC", "AllocatedTo"), ("AD-VERIF", "Verification")]


class ReqIFError(ValueError):
    pass


def _id(*parts: str) -> str:
    return "_" + str(uuid.uuid5(ID_NS, "/".join(parts)))


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _ts(value: str | None = None) -> str:
    if value:
        try:
            return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _classification(rec: dict) -> str:
    p = rec["payload"]
    if p.get("asil"):
        return f"ASIL {p['asil']}" if p["asil"] != "QM" else "QM"
    if p.get("risk") is not None and rec["module"] == "tara":
        return f"risk {p['risk']}"
    if rec["module"] == "adas":
        return "release-blocking" if p.get("release_blocking") else "exploratory"
    return ""


def _text(rec: dict) -> tuple[str, str]:
    p = rec["payload"]
    if rec["module"] == "adas":
        return f"{p['id']} {p['title']}", p["text"]
    text = p.get("text") or p.get("title") or rec["ref"]
    return (text[:80] + ("…" if len(text) > 80 else "")), text


def _value(parent: ET.Element, def_id: str, value: str, ts: str) -> None:
    v = ET.SubElement(parent, _q("ATTRIBUTE-VALUE-STRING"), {"THE-VALUE": value})
    ET.SubElement(ET.SubElement(v, _q("DEFINITION")), _q("ATTRIBUTE-DEFINITION-STRING-REF")).text = def_id


def export(project: str, records: list[dict], links: list[dict], title: str = "") -> str:
    """records: platform records of the EXPORT_KINDS; links: trace links (only those between exported records are kept)."""
    ET.register_namespace("", NS)
    now = _ts()
    root = ET.Element(_q("REQ-IF"))
    head = ET.SubElement(ET.SubElement(root, _q("THE-HEADER")), _q("REQ-IF-HEADER"), {"IDENTIFIER": _id(project, "header", now)})
    for tag, val in (("COMMENT", "Exported by automotive-eng-ai. Generated drafts must be reviewed; see each Status."),
                     ("CREATION-TIME", now), ("REQ-IF-TOOL-ID", "automotive-eng-ai"), ("REQ-IF-VERSION", "1.0"),
                     ("SOURCE-TOOL-ID", "automotive-eng-ai"), ("TITLE", title or f"{project}: safety, security and ADAS requirements")):
        ET.SubElement(head, _q(tag)).text = val
    content = ET.SubElement(ET.SubElement(root, _q("CORE-CONTENT")), _q("REQ-IF-CONTENT"))
    dt = ET.SubElement(content, _q("DATATYPES"))
    ET.SubElement(dt, _q("DATATYPE-DEFINITION-STRING"), {"IDENTIFIER": "DT-STRING", "LAST-CHANGE": now, "LONG-NAME": "String",
                                                          "MAX-LENGTH": "32000"})
    types = ET.SubElement(content, _q("SPEC-TYPES"))
    sot = ET.SubElement(types, _q("SPEC-OBJECT-TYPE"), {"IDENTIFIER": "SOT-REQUIREMENT", "LAST-CHANGE": now, "LONG-NAME": "Requirement"})
    attrs = ET.SubElement(sot, _q("SPEC-ATTRIBUTES"))
    for aid, name in ATTRS:
        ad = ET.SubElement(attrs, _q("ATTRIBUTE-DEFINITION-STRING"), {"IDENTIFIER": aid, "LAST-CHANGE": now, "LONG-NAME": name})
        ET.SubElement(ET.SubElement(ad, _q("TYPE")), _q("DATATYPE-DEFINITION-STRING-REF")).text = "DT-STRING"
    ET.SubElement(types, _q("SPEC-RELATION-TYPE"), {"IDENTIFIER": "SRT-TRACE", "LAST-CHANGE": now, "LONG-NAME": "Trace"})
    ET.SubElement(types, _q("SPECIFICATION-TYPE"), {"IDENTIFIER": "ST-SPEC", "LAST-CHANGE": now, "LONG-NAME": "Specification"})
    objs = ET.SubElement(content, _q("SPEC-OBJECTS"))
    ids = {}
    for rec in records:
        name, text = _text(rec)
        oid = ids[rec["ref"]] = _id(project, rec["ref"])
        p = rec["payload"]
        verif = p.get("verification") or []
        o = ET.SubElement(objs, _q("SPEC-OBJECT"), {"IDENTIFIER": oid, "LAST-CHANGE": _ts(rec.get("updated_at")), "LONG-NAME": name[:250]})
        vals = ET.SubElement(o, _q("VALUES"))
        for aid, value in (("AD-FOREIGN-ID", rec["ref"]), ("AD-NAME", name), ("AD-TEXT", text), ("AD-MODULE", rec["module"]),
                           ("AD-KIND", rec["kind"]), ("AD-STATUS", rec["status"]), ("AD-CLASS", _classification(rec)),
                           ("AD-ALLOC", str(p.get("allocated_to") or "")),
                           ("AD-VERIF", ", ".join(verif) if isinstance(verif, list) else str(verif))):
            _value(vals, aid, value, now)
        ET.SubElement(ET.SubElement(o, _q("TYPE")), _q("SPEC-OBJECT-TYPE-REF")).text = "SOT-REQUIREMENT"
    rels = ET.SubElement(content, _q("SPEC-RELATIONS"))
    n_rel = 0
    for link in links:
        if link["src"] in ids and link["dst"] in ids:
            r = ET.SubElement(rels, _q("SPEC-RELATION"), {"IDENTIFIER": _id(project, "rel", link["src"], link["dst"], link["rel"]),
                                                          "LAST-CHANGE": now, "LONG-NAME": link["rel"]})
            ET.SubElement(ET.SubElement(r, _q("TYPE")), _q("SPEC-RELATION-TYPE-REF")).text = "SRT-TRACE"
            ET.SubElement(ET.SubElement(r, _q("SOURCE")), _q("SPEC-OBJECT-REF")).text = ids[link["src"]]
            ET.SubElement(ET.SubElement(r, _q("TARGET")), _q("SPEC-OBJECT-REF")).text = ids[link["dst"]]
            n_rel += 1
    if not n_rel:
        content.remove(rels)
    specs = ET.SubElement(content, _q("SPECIFICATIONS"))
    for module, kind, label in EXPORT_KINDS:
        members = [r for r in records if r["module"] == module and r["kind"] == kind]
        if not members:
            continue
        sp = ET.SubElement(specs, _q("SPECIFICATION"), {"IDENTIFIER": _id(project, "spec", module, kind), "LAST-CHANGE": now, "LONG-NAME": label})
        ET.SubElement(ET.SubElement(sp, _q("TYPE")), _q("SPECIFICATION-TYPE-REF")).text = "ST-SPEC"
        children = ET.SubElement(sp, _q("CHILDREN"))
        for r in members:
            h = ET.SubElement(children, _q("SPEC-HIERARCHY"), {"IDENTIFIER": _id(project, "h", module, kind, r["ref"]), "LAST-CHANGE": now})
            ET.SubElement(ET.SubElement(h, _q("OBJECT")), _q("SPEC-OBJECT-REF")).text = ids[r["ref"]]
    if not len(specs):
        content.remove(specs)
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# ----------------------------------------------------------------------------- import
def _read(content: bytes, filename: str) -> bytes:
    if len(content) > MAX_BYTES:
        raise ReqIFError("file too large")
    if filename.lower().endswith(".reqifz") or content[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                names = [i for i in z.infolist() if i.filename.lower().endswith(".reqif")]
                if len(names) != 1:
                    raise ReqIFError(f".reqifz must contain exactly one .reqif file, found {len(names)}")
                if names[0].file_size > MAX_BYTES:
                    raise ReqIFError("archive member too large")
                return z.read(names[0])
        except zipfile.BadZipFile as e:
            raise ReqIFError("not a valid .reqifz archive") from e
    return content


def _plain(el: ET.Element | None) -> str:
    if el is None:
        return ""
    txt = " ".join(t.strip() for t in el.itertext() if t.strip())
    return re.sub(r"\s+", " ", txt).strip()


def parse(content: bytes, filename: str = "import.reqif") -> dict:
    raw = _read(content, filename)
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw, re.I):
        raise ReqIFError("XML with DOCTYPE/ENTITY declarations is rejected (entity expansion risk)")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ReqIFError(f"not well-formed XML: {e}") from e
    if root.tag != _q("REQ-IF"):
        raise ReqIFError("not a ReqIF document (root element REQ-IF in the ReqIF 1.0/1.2 namespace expected)")
    header = root.find(f"{_q('THE-HEADER')}/{_q('REQ-IF-HEADER')}")
    content_el = root.find(f"{_q('CORE-CONTENT')}/{_q('REQ-IF-CONTENT')}")
    if content_el is None:
        raise ReqIFError("REQ-IF-CONTENT missing")
    enum_values = {}
    for ev in content_el.iter(_q("ENUM-VALUE")):
        enum_values[ev.get("IDENTIFIER")] = ev.get("LONG-NAME") or ev.get("IDENTIFIER")
    attr_names = {}
    for tag in ("STRING", "XHTML", "ENUMERATION", "INTEGER", "REAL", "BOOLEAN", "DATE"):
        for ad in content_el.iter(_q(f"ATTRIBUTE-DEFINITION-{tag}")):
            attr_names[ad.get("IDENTIFIER")] = ad.get("LONG-NAME") or ad.get("IDENTIFIER")
    objects = {}
    for o in content_el.iter(_q("SPEC-OBJECT")):
        if len(objects) >= MAX_OBJECTS:
            raise ReqIFError(f"more than {MAX_OBJECTS} spec objects")
        attrs = {}
        values = o.find(_q("VALUES"))
        for v in list(values) if values is not None else []:
            kind = v.tag.replace(_q("ATTRIBUTE-VALUE-"), "")
            dref = v.find(f"{_q('DEFINITION')}/*")
            name = attr_names.get(dref.text.strip() if dref is not None and dref.text else "", "unknown")
            if kind == "XHTML":
                val = _plain(v.find(_q("THE-VALUE")))
            elif kind == "ENUMERATION":
                val = ", ".join(enum_values.get((r.text or "").strip(), (r.text or "").strip()) for r in v.iter(_q("ENUM-VALUE-REF")))
            else:
                val = v.get("THE-VALUE", "")
            attrs[name] = val[:20000]
        oid = o.get("IDENTIFIER")
        if not oid:
            continue
        title = attrs.get("ReqIF.Name") or attrs.get("ReqIF.ChapterName") or o.get("LONG-NAME") or ""
        text = attrs.get("ReqIF.Text") or attrs.get("Object Text") or attrs.get("Text") or attrs.get("Description") or ""
        if not title:
            title = (text[:80] + "…") if len(text) > 80 else text
        key = attrs.get("ReqIF.ForeignID") or attrs.get("ID") or attrs.get("Absolute Number") or ""
        objects[oid] = {"identifier": oid, "key": key[:60], "title": title[:250], "text": text[:20000],
                        "attributes": {k: v for k, v in attrs.items() if k not in ("ReqIF.Text", "ReqIF.Name")}}
    order = [r.text.strip() for r in content_el.iter(_q("SPEC-OBJECT-REF")) if r.text and r.text.strip() in objects]
    ordered = list(dict.fromkeys([oid for oid in order if oid in objects] + list(objects)))
    reqs = []
    for i, oid in enumerate(ordered):
        o = objects[oid]
        o["position"] = i
        o["hash"] = hashlib.sha256(f"{o['title']}\x00{o['text']}\x00{sorted(o['attributes'].items())}".encode()).hexdigest()[:16]
        reqs.append(o)
    hdr = {}
    if header is not None:
        for tag in ("TITLE", "CREATION-TIME", "SOURCE-TOOL-ID", "REQ-IF-TOOL-ID", "COMMENT"):
            el = header.find(_q(tag))
            hdr[tag.lower().replace("-", "_")] = (el.text or "").strip()[:200] if el is not None else ""
    return {"header": hdr, "requirements": reqs, "sha256": hashlib.sha256(raw).hexdigest()}
