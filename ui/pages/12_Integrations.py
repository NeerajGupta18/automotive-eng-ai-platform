"""Phase 5: enterprise integration hub: signed webhooks, Jira tickets, ReqIF requirement exchange, MCP."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import common
from common import api, do, raw_get, status_badge

CHANGE = {"new": "🆕 new", "modified": "✏️ modified", "removed": "🗑 removed"}
DELIVERY = {"delivered": "✅ delivered", "dry_run": "🧪 dry run (not sent)", "pending": "⏳ pending", "retry": "🔁 retry",
            "failed": "❌ failed", "cancelled": "⏹ cancelled"}



def fmt_summary(sm: dict) -> str:
    return ", ".join(f"{sm[k]} {k}" for k in ("new", "modified", "removed", "unchanged") if sm.get(k))


common.setup("Integrations")
project = common.sidebar("Module: **Enterprise integration** (Phase 5). Events to other systems, Jira tickets, ReqIF with "
                         "OEM/supplier tools, MCP for AI assistants.")
st.title("Enterprise integration")
if not project:
    st.info("Create a project workspace in the sidebar.")
    st.stop()
I = f"/projects/{project}/integrations"
cfg = api("GET", f"{I}/config") or {"live": False, "mode": "?", "jira": {}, "webhooks": {"default_events": []}}
(st.success if cfg["live"] else st.info)(f"Mode: **{cfg['mode']}**" + ("" if cfg["live"] else " (set INTEGRATIONS_LIVE=1 to send)."))

t_req, t_wh, t_tk, t_ai = st.tabs(["1 · ReqIF requirement exchange", "2 · Webhooks (events)", "3 · Jira tickets",
                                   "4 · AI assistants (MCP) and CI"])

# ------------------------------------------------------------------ 1 ReqIF
with t_req:
    st.markdown("Exchange requirements with OEM and supplier tools (DOORS, Polarion, codebeamer, Jama) in **ReqIF 1.2**. "
                "Re-importing a newer version shows what the customer changed and flags the platform items linked to it.")
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Import from a partner")
        up = st.file_uploader("ReqIF file (.reqif / .reqifz)", type=["reqif", "reqifz", "xml"], key="reqif_up")
        st.caption("Try data/samples/integration/oem_aeb_requirements_v1.reqif, accept and link, then import v2.")
        if up and st.button("Import"):
            r = api("POST", f"{I}/reqif/import", files={"file": (up.name, up.getvalue())})
            if r:
                st.success(f"{r['ref']}: {fmt_summary(r['payload']['summary'])}" + (f" · flagged for re-review: {', '.join(r['payload']['impacted'])}"
                                                                                  if r["payload"]["impacted"] else ""))
    with c2:
        st.subheader("Export to a partner")
        st.caption("Safety goals and requirements, cybersecurity goals and requirements, ADAS requirements, with trace links.")
        if st.button("Prepare ReqIF export", help="Exports are recorded in the audit log"):
            r = raw_get(f"{I}/reqif/export")
            if r is not None:
                st.download_button("⬇ Download ReqIF", r.content, file_name=f"{project}_requirements.reqif", mime="application/xml")
            else:
                st.caption("Nothing to export yet (no safety, cybersecurity or ADAS requirements).")
    imports = api("GET", f"{I}/reqif/imports") or []
    for imp in imports[:3]:
        p = imp["payload"]
        with st.expander(f"{imp['ref']} · {p['title']} · {fmt_summary(p['summary'])} · {imp['created_at'][:16]} by {imp['created_by']}",
                         expanded=imp is imports[0] and any(p["summary"][k] for k in ("modified", "removed"))):
            for ch in p["changes"]:
                st.markdown(f"{CHANGE[ch['change']]} **{ch['key'] or ch['ref']}** {ch['title']} (`{ch['ref']}`)")
                if ch["change"] == "modified":
                    st.markdown(f"> before: {ch['before']}\n>\n> after: **{ch['after']}**")
            if p["impacted"]:
                st.warning(f"Flagged for re-review: {', '.join(p['impacted'])} (see *Traceability*)")
    xr = api("GET", f"{I}/reqif/requirements") or []
    if xr:
        st.subheader("Customer requirements")
        st.dataframe(pd.DataFrame([{"ref": x["ref"], "id": x["payload"]["key"], "title": x["payload"]["title"],
                                    "status": status_badge(x["status"]), "last change": CHANGE.get(x["payload"].get("change"), ""),
                                    "satisfied by": ", ".join(x["links"]), "priority": x["payload"]["attributes"].get("Priority", ""),
                                    "text": x["payload"]["text"]} for x in xr]), hide_index=True, width="stretch")
        pend = [x for x in xr if x["status"] in ("proposed", "suspect")]
        if pend:
            st.markdown(f"**{len(pend)} waiting for review** (reviewer role)")
            sel = st.selectbox("Review", [x["ref"] for x in pend], format_func=lambda r: next(
                f"{r} {x['payload']['key']} {x['payload']['title']} ({x['payload'].get('change')})" for x in pend if x["ref"] == r))
            cm = st.text_input("Comment", key="xr_cm")
            a1, a2 = st.columns(2)
            if a1.button("Accept"):
                do("POST", f"{I}/reqif/requirements/{sel}/decision", json={"decision": "approved", "comment": cm})
            if a2.button("Reject"):
                do("POST", f"{I}/reqif/requirements/{sel}/decision", json={"decision": "rejected", "comment": cm})
        with st.form("link"):
            st.markdown("**Link a customer requirement to the platform item that satisfies it**")
            l1, l2 = st.columns(2)
            xr_ref = l1.selectbox("Customer requirement", [x["ref"] for x in xr if x["status"] != "rejected"],
                                  format_func=lambda r: next(f"{r} {x['payload']['key']}" for x in xr if x["ref"] == r))
            target = l2.text_input("Platform reference (e.g. AR-005, FSR-001, TC-012)")
            if st.form_submit_button("Link") and target:
                do("POST", f"{I}/reqif/links", json={"xr_ref": xr_ref, "target_ref": target.strip()})

# ------------------------------------------------------------------ 2 webhooks
with t_wh:
    st.markdown("Every state change is already in the tamper-evident audit log; webhooks forward the selected events to other "
                "systems (CI, ALM, chat, a data lake), **signed with HMAC-SHA256** and retried with back-off.")
    hooks = api("GET", f"{I}/webhooks") or []
    if hooks:
        st.dataframe(pd.DataFrame([{"id": h["id"], "url": h["url"], "events": ", ".join(h["events"]), "description": h["description"],
                                    "deliveries": ", ".join(f"{k}: {v}" for k, v in h["deliveries"].items()), "by": h["created_by"]}
                                   for h in hooks]), hide_index=True, width="stretch")
        h1, h2, h3 = st.columns(3)
        wid = h1.selectbox("Webhook", [h["id"] for h in hooks])
        if h2.button("Send a test (ping)"):
            r = api("POST", f"{I}/webhooks/{wid}/ping")
            if r:
                st.info(f"{DELIVERY.get(r['status'], r['status'])} · {r.get('last_error') or ''} · signature {r['signature'][:24]}…")
        if h3.button("Remove", help="Project administrators only"):
            do("DELETE", f"{I}/webhooks/{wid}")
    with st.expander("➕ Add a webhook (project administrator)"):
        with st.form("wh"):
            url = st.text_input("Receiver URL (https)", placeholder="https://ci.example.com/hooks/aeai")
            ev = st.multiselect("Events", cfg["webhooks"]["default_events"] + ["*"], default=cfg["webhooks"]["default_events"])
            desc = st.text_input("Description")
            if st.form_submit_button("Add"):
                r = api("POST", f"{I}/webhooks", json={"url": url, "events": ev, "description": desc})
                if r:
                    st.success(f"{r['id']} added. Signing secret (shown only once): `{r['secret']}`")
        st.caption(f"Policy: public https targets only{'; allowed hosts: ' + ', '.join(cfg['webhooks']['allow_hosts']) if cfg['webhooks']['allow_hosts'] else ''}"
                   f"{'; private addresses allowed' if cfg['webhooks']['allow_private'] else ' (private and loopback addresses are blocked)'}.")
    if st.button("Deliver pending events now"):
        r = api("POST", f"{I}/dispatch")
        if r:
            st.info(f"queued {r['queued']} · delivered {r['delivered']} · dry run {r['dry_run']} · retry {r['retry']} · failed {r['failed']}")
    dl = api("GET", f"{I}/deliveries") or []
    if dl:
        st.dataframe(pd.DataFrame([{"#": d["id"], "webhook": d["webhook_id"], "event": d["event"], "status": DELIVERY.get(d["status"], d["status"]),
                                    "attempts": d["attempts"], "HTTP": d["last_status"], "error": d["last_error"] or "",
                                    "next attempt": (d["next_attempt_at"] or "")[:16] if d["status"] == "retry" else ""} for d in dl]),
                     hide_index=True, width="stretch")
        with st.expander("Latest event body and signature"):
            st.json(dl[0]["body"])
            st.code(dl[0]["signature"] or "", language="text")
    with st.expander("How a receiver verifies the signature"):
        st.code('''import hashlib, hmac, time

def verify(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:          # reject replays
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)      # headers: X-AEAI-Timestamp, X-AEAI-Signature-256''', language="python")

# ------------------------------------------------------------------ 3 Jira
with t_tk:
    j = cfg["jira"]
    st.markdown(f"Jira: **{'configured' if j.get('configured') else 'not configured'}**"
                + (f" · {j['base_url']} · project {j['project_key']} · issue type {j['issue_type']} · auth {j['auth']}" if j.get("base_url") else "")
                + ". Tickets are idempotent per source and summary, so re-running the agent creates no duplicates.")
    tks = api("GET", f"{I}/tickets") or []
    if tks:
        st.dataframe(pd.DataFrame([{"ref": t["ref"], "status": t["status"], "summary": t["payload"]["summary"], "priority": t["payload"]["priority"],
                                    "source": t["payload"]["source_ref"], "Jira": t["payload"].get("external_key") or "",
                                    "note": t["payload"].get("last_error") or "", "by": t["created_by"]} for t in tks]),
                     hide_index=True, width="stretch")
        pick = st.selectbox("Ticket", [t["ref"] for t in tks])
        t = next(x for x in tks if x["ref"] == pick)
        st.text(t["payload"]["description"])
        with st.expander("Exact request body for Jira (POST /rest/api/2/issue)"):
            st.json(t["payload"]["request"])
        if t["status"] in ("failed", "dry_run", "queued") and st.button("Send / retry"):
            do("POST", f"{I}/tickets/{pick}/send")
    with st.expander("➕ Create a ticket"):
        with st.form("tk"):
            s = st.text_input("Summary")
            d = st.text_area("Description")
            src = st.text_input("Source reference (optional, e.g. F-012)")
            pr = st.selectbox("Priority", ["Medium", "High", "Highest", "Low"])
            if st.form_submit_button("Create") and s:
                do("POST", f"{I}/tickets", json={"summary": s, "description": d, "source_ref": src.strip(), "priority": pr})

# ------------------------------------------------------------------ 4 MCP and CI
with t_ai:
    st.subheader("AI assistants: Model Context Protocol")
    st.markdown("`scripts/mcp_server.py` exposes release readiness, traceability impact, the re-review worklist, document Q&A, "
                "ADAS status, vulnerabilities and agent runs to any MCP client (Claude Desktop, IDE assistants, in-house agents). "
                "It calls this platform's API with the token you give it, so the assistant has exactly that user's permissions, "
                "and agent runs it starts still stop at human checkpoints here.")
    st.code(json.dumps({"mcpServers": {"automotive-eng-ai": {"command": "python", "args": ["/path/to/repo/scripts/mcp_server.py"],
                                                            "env": {"AEAI_API_URL": "http://localhost:8000", "AEAI_PROJECT": project,
                                                                    "AEAI_API_TOKEN": "<token of a viewer or engineer>"}}}}, indent=2),
            language="json")
    st.subheader("CI/CD release gate")
    st.code(f"python scripts/release_gate.py --project {project} --api https://<platform> --report readiness.md\n"
            f"# exit 0 = GO, 1 = not releasable, 2 = platform unreachable", language="bash")
