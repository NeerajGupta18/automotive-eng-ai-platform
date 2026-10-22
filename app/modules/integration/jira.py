"""Jira connector: issue payloads for blocking items, findings and agent recommendations.

Uses the REST API v2 issue endpoint (POST /rest/api/2/issue), which Jira Cloud and Jira Data Center both
accept. Jira Cloud authenticates with an account e-mail and API token (basic auth); Data Center with a
personal access token (bearer). Credentials come from environment variables only and are never stored in the
database or returned by the API. Without INTEGRATIONS_LIVE=1 and a complete configuration, tickets are kept as
dry runs that show exactly what would be sent."""
from __future__ import annotations

import re

LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")
PRIORITIES = ("Highest", "High", "Medium", "Low", "Lowest")


class JiraError(RuntimeError):
    pass


def payload(project_key: str, issue_type: str, summary: str, description: str, labels: list[str],
            priority: str | None = None) -> dict:
    fields = {"project": {"key": project_key or "AEAI"}, "issuetype": {"name": issue_type or "Task"},
              "summary": " ".join(summary.split())[:250], "description": description[:30000],
              "labels": [LABEL_RE.sub("-", x)[:50] for x in labels if x][:10]}
    if priority in PRIORITIES:
        fields["priority"] = {"name": priority}
    return {"fields": fields}


def create_issue(base_url: str, user: str, token: str, body: dict, timeout: int = 10) -> dict:
    import requests
    hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
    auth = (user, token) if user else None
    if not user:
        hdrs["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(f"{base_url}/rest/api/2/issue", json=body, headers=hdrs, auth=auth, timeout=timeout,
                          allow_redirects=False)
    except requests.RequestException as e:
        raise JiraError(f"{type(e).__name__}: {str(e)[:200]}") from e
    if r.status_code != 201:
        detail = r.text[:300]
        try:
            j = r.json()
            detail = "; ".join(j.get("errorMessages", []) + [f"{k}: {v}" for k, v in (j.get("errors") or {}).items()]) or detail
        except ValueError:
            pass
        raise JiraError(f"Jira answered HTTP {r.status_code}: {detail}")
    j = r.json()
    key = j.get("key", "")
    return {"key": key, "id": j.get("id", ""), "url": f"{base_url}/browse/{key}" if key else ""}
