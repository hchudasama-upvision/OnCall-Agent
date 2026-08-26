import os
from dataclasses import dataclass, field
from typing import List, Optional

import requests

"""
Read-only Jira Cloud REST API client (basic auth: account email + API
token), same purpose as Github_Agent/github_client.py: let a specialist
check for an EXISTING ticket that already explains or tracks this exact
issue, instead of only ever citing a case-library note frozen at whenever it
was written. Same repo-wide "never fabricate" contract applies at the
caller — every key/URL returned here is real (came back from a real Jira
API response), so once it lands in the tool-result corpus it becomes a
legitimately citable link (investigate.py's `_allowed_urls` scans that
corpus generically; nothing Jira-specific was needed there).

Read-only only: every function below is a GET. Nothing here can create,
comment on, or transition an issue — the same boundary every other tool
client in this repo holds (CLAUDE.md non-negotiable #3, no action
execution).
"""


def _site() -> str:
    site = os.environ.get("JIRA_SITE")
    if not site:
        raise RuntimeError("JIRA_SITE not set")
    return site.rstrip("/")


def _auth() -> requests.auth.HTTPBasicAuth:
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not email or not token:
        raise RuntimeError("JIRA_EMAIL / JIRA_API_TOKEN not set")
    return requests.auth.HTTPBasicAuth(email, token)


def _get(path: str, params: Optional[dict] = None) -> dict:
    res = requests.get(f"{_site()}{path}", auth=_auth(), params=params,
                       headers={"Accept": "application/json"}, timeout=20)
    if not res.ok:
        raise RuntimeError(f"Jira API call failed: {res.status_code} {res.reason} — {path} — {res.text[:300]}")
    return res.json()


def _adf_to_text(node) -> str:
    """Minimal Atlassian Document Format -> plain text: walk `content`,
    keep `text` node values, add a newline after block-level nodes. Good
    enough for a model to read a description/comment; not a full renderer."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    text = "".join(_adf_to_text(c) for c in (node.get("content") or []))
    if node.get("type") in ("paragraph", "heading", "listItem", "codeBlock"):
        text += "\n"
    return text


_MAX_TEXT_CHARS = 4000


def _body_text(adf: Optional[dict]) -> str:
    text = _adf_to_text(adf).strip() if adf else ""
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + f"\n... [truncated, {len(text)} chars total]"
    return text


@dataclass
class IssueSummary:
    key: str
    summary: str
    status: str
    updated: str
    html_url: str


def _jql_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"')


def search_issues(query: str, limit: int = 10) -> List[IssueSummary]:
    """Full-text search (summary + description + comments) for a literal
    phrase — e.g. an alert name, an error code, a hostname/cluster — to find
    a ticket that already tracks this issue. Newest-updated first."""
    jql = f'text ~ "{_jql_escape(query)}" ORDER BY updated DESC'
    payload = _get("/rest/api/3/search/jql", params={
        "jql": jql, "maxResults": min(int(limit), 25), "fields": "summary,status,updated"})
    site = _site()
    return [
        IssueSummary(
            key=i["key"], summary=(i.get("fields") or {}).get("summary", ""),
            status=((i.get("fields") or {}).get("status") or {}).get("name", ""),
            updated=(i.get("fields") or {}).get("updated", ""),
            html_url=f"{site}/browse/{i['key']}",
        )
        for i in payload.get("issues", [])
    ]


@dataclass
class IssueDetail:
    key: str
    summary: str
    status: str
    assignee: str
    reporter: str
    created: str
    updated: str
    description: str
    html_url: str
    comment_count: int


def get_issue(key: str) -> IssueDetail:
    """Full detail of one issue by key (e.g. 'NOC-13707') — the real
    current status/assignee/description, not a case-library note's memory
    of it from whenever that note was written."""
    payload = _get(f"/rest/api/3/issue/{key}", params={
        "fields": "summary,status,assignee,reporter,created,updated,description,comment"})
    fields = payload.get("fields") or {}
    assignee = (fields.get("assignee") or {}).get("displayName") or "(unassigned)"
    reporter = (fields.get("reporter") or {}).get("displayName") or "(unknown)"
    comments = ((fields.get("comment") or {}).get("comments")) or []
    return IssueDetail(
        key=payload["key"], summary=fields.get("summary", ""),
        status=(fields.get("status") or {}).get("name", ""),
        assignee=assignee, reporter=reporter,
        created=fields.get("created", ""), updated=fields.get("updated", ""),
        description=_body_text(fields.get("description")),
        html_url=f"{_site()}/browse/{payload['key']}",
        comment_count=len(comments),
    )


@dataclass
class CommentInfo:
    author: str
    created: str
    body: str


def list_comments(key: str, limit: int = 5) -> List[CommentInfo]:
    """Most recent comments on an issue, newest first — often where the
    actual root cause / resolution / handoff is written, not the
    description."""
    payload = _get(f"/rest/api/3/issue/{key}/comment", params={"maxResults": 100})
    comments = payload.get("comments") or []
    comments.sort(key=lambda c: c.get("created", ""), reverse=True)
    return [
        CommentInfo(
            author=(c.get("author") or {}).get("displayName") or "(unknown)",
            created=c.get("created", ""), body=_body_text(c.get("body")),
        )
        for c in comments[:limit]
    ]
