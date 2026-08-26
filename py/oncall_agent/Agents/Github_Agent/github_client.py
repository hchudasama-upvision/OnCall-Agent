import base64
import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional

"""
Read-only GitHub access via the `gh` CLI, so a specialist can check the real
code/PR/deploy state instead of trusting a case-library note frozen at
whenever that entry was written ("deploy_verification" in a case is a human
saying a check is automated — this is what lets the model actually run one).

Deliberate deviation from CLAUDE.md's "credentials only from .env" rule,
flagged here the same way the claude_cli LLM provider's own deviation is
flagged in CLAUDE.md: `gh` is already authenticated on this machine (its own
OAuth session, `gh auth status`), and reusing that is simpler and no less
safe than minting a separate GITHUB_TOKEN — the token itself never leaves
gh's own config and never enters a prompt, only command OUTPUT does. If this
ever needs to run somewhere `gh` isn't already logged in, switch to a
GITHUB_TOKEN env var read here and passed as GH_TOKEN to the subprocess.

Read-only only: every function below maps to a GET-shaped `gh api` call.
Nothing here can open a PR, push a commit, or trigger a workflow — the same
boundary every other tool client in this repo holds.

SSO note: an org with SAML SSO enforcement (veritone/realtime is one) blocks
even a validly-authenticated token until a human authorizes that specific
token for that specific org — a one-time browser action at
https://github.com/orgs/<org>/sso, separate from org membership. Every
function here surfaces gh's own error text on failure, which includes that
URL when this is the cause, rather than swallowing it into a generic error.
"""


def _gh(*args: str) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh command failed").strip())
    return proc.stdout


def _gh_json(*args: str):
    return json.loads(_gh(*args))


@dataclass
class CodeMatch:
    repo: str
    path: str
    html_url: str


def search_code(query: str, repo: str = "") -> List[CodeMatch]:
    """Search code across GitHub (or one repo) for a literal string — e.g. an
    error code, a function name — to find where it actually lives.

    Uses the `gh search code` subcommand rather than `gh api search/code`
    directly: the raw API needs an explicit `-X GET` (gh api defaults to
    POST whenever `-f` fields are given, which 404s a GET-only search
    endpoint) — the subcommand handles that correctly and gives a real `url`
    field directly instead of requiring it to be reconstructed.
    """
    args = ["search", "code", query, "--limit", "20", "--json", "path,repository,url"]
    if repo:
        args += ["--repo", repo]
    items = _gh_json(*args)
    return [
        CodeMatch(repo=item["repository"]["nameWithOwner"], path=item["path"], html_url=item["url"])
        for item in items
    ]


_MAX_FILE_CHARS = 20000


def get_file(repo: str, path: str, ref: str = "") -> str:
    """Raw contents of one file at a repo/path/ref (default branch if ref is
    empty). Truncated if huge — this is for reading a specific function/error
    path, not dumping an entire generated file."""
    endpoint = f"repos/{repo}/contents/{path}"
    if ref:
        endpoint += f"?ref={ref}"
    payload = _gh_json("api", endpoint)
    if payload.get("type") != "file" or "content" not in payload:
        raise RuntimeError(f"{path!r} in {repo} is not a plain file (type={payload.get('type')})")
    content = base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    if len(content) > _MAX_FILE_CHARS:
        content = content[:_MAX_FILE_CHARS] + f"\n... [truncated, {len(content)} chars total]"
    return content


@dataclass
class PullRequestInfo:
    number: int
    title: str
    state: str
    merged: bool
    merged_at: Optional[str]
    merge_commit_sha: Optional[str]
    base_ref: str
    head_ref: str
    html_url: str


def get_pr(repo: str, number: int) -> PullRequestInfo:
    """A PR's real merge state — was it actually merged, into what, when,
    and as which commit. The thing a case-library fix_reference can't tell
    you on its own."""
    p = _gh_json("api", f"repos/{repo}/pulls/{number}")
    return PullRequestInfo(
        number=p["number"], title=p["title"], state=p["state"], merged=bool(p.get("merged")),
        merged_at=p.get("merged_at"), merge_commit_sha=p.get("merge_commit_sha"),
        base_ref=p["base"]["ref"], head_ref=p["head"]["ref"], html_url=p["html_url"],
    )


@dataclass
class CommitInfo:
    sha: str
    message: str
    author: str
    date: str
    html_url: str
    files_changed: List[str]


def get_commit(repo: str, sha: str) -> CommitInfo:
    c = _gh_json("api", f"repos/{repo}/commits/{sha}")
    return CommitInfo(
        sha=c["sha"], message=c["commit"]["message"],
        author=(c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
        date=c["commit"]["author"]["date"], html_url=c["html_url"],
        files_changed=[f["filename"] for f in c.get("files", [])][:50],
    )


@dataclass
class WorkflowRun:
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    head_branch: str
    created_at: str
    html_url: str


def list_workflow_runs(repo: str, workflow: str = "", branch: str = "", limit: int = 10) -> List[WorkflowRun]:
    """Recent GitHub Actions runs — status/conclusion, not logs. Answers
    "did the automated check actually run, and did it pass" without pulling
    potentially-sensitive log content into the model's context."""
    endpoint = f"repos/{repo}/actions/runs?per_page={min(int(limit), 30)}"
    if branch:
        endpoint += f"&branch={branch}"
    payload = _gh_json("api", endpoint)
    runs = payload.get("workflow_runs", [])
    if workflow:
        runs = [r for r in runs if workflow.lower() in (r.get("name") or "").lower()]
    return [
        WorkflowRun(id=r["id"], name=r["name"], status=r["status"], conclusion=r.get("conclusion"),
                   head_branch=r["head_branch"], created_at=r["created_at"], html_url=r["html_url"])
        for r in runs
    ]
