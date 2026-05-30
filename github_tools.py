"""GitHub repository tools — capability-named ops on repos.

Auth is internal: prefer an installation token from the GitHub App
(see ``tools.github_app_workspace._get_app_token``) when configured;
otherwise fall back to ``GITHUB_TOKEN`` / ``GH_TOKEN`` (classic PAT).
Agents never see the auth choice — they just call the tool. This is the
encapsulation that prevents the "hand-roll JWT through execute_code"
anti-pattern observed before these tools existed.

Scope:
- App auth → repos under the App's installation.
- PAT auth → whatever the PAT grants.

Tool catalogue:
  github_repo_list    — list repos accessible to the agent (filter by org)
  github_repo_view    — fetch metadata for a single repo
  github_repo_delete  — DELETE /repos/{owner}/{repo}  (irreversible)
  github_repo_create  — create a new repo (private by default)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

import httpx

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _get_token() -> Optional[str]:
    """Return a usable GitHub auth token. App installation first, then PAT."""
    try:
        from tools.github_app_workspace import _get_app_token  # type: ignore
        tok = _get_app_token()
        if tok:
            return tok
    except Exception as exc:
        logger.debug("github app token lookup failed: %s", exc)
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _credentials_available() -> bool:
    """Toolset check: agent has at least one usable auth path."""
    if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return True
    try:
        from tools.github_app_workspace import is_configured  # type: ignore
        return bool(is_configured())
    except Exception:
        return False


def _api(method: str, path: str, *, json_body: Optional[dict] = None) -> httpx.Response:
    token = _get_token()
    if not token:
        raise RuntimeError(
            "no GitHub credentials available — set GITHUB_TOKEN or configure "
            "the GitHub App (operator-side, not agent-asks-user)"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com{path}"
    with httpx.Client(timeout=30) as client:
        return client.request(method, url, headers=headers, json=json_body)


def _validate_repo(repo: str) -> Optional[str]:
    if not repo or not _REPO_RE.match(repo):
        return f"invalid repo '{repo}' — expected 'owner/name' format"
    return None


# ---------- Handlers ---------------------------------------------------------


def github_repo_list(org: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        return tool_error("limit must be an integer 1..100")

    if org:
        path = f"/orgs/{org}/repos?per_page={limit}&sort=updated"
    else:
        # /installation/repositories is the App-token endpoint; for PAT auth
        # it returns 404, in which case fall back to /user/repos.
        path = f"/installation/repositories?per_page={limit}"

    try:
        resp = _api("GET", path)
    except RuntimeError as exc:
        return tool_error(str(exc))

    if resp.status_code == 404 and not org:
        try:
            resp = _api("GET", f"/user/repos?per_page={limit}&sort=updated")
        except RuntimeError as exc:
            return tool_error(str(exc))

    if resp.status_code != 200:
        return tool_error(f"github API {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    repos = data.get("repositories", []) if isinstance(data, dict) else data
    return {
        "ok": True,
        "count": len(repos),
        "repos": [
            {
                "full_name": r["full_name"],
                "private": r.get("private"),
                "description": r.get("description"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
            for r in repos
        ],
    }


def github_repo_view(repo: str) -> Dict[str, Any]:
    err = _validate_repo(repo)
    if err:
        return tool_error(err)
    try:
        resp = _api("GET", f"/repos/{repo}")
    except RuntimeError as exc:
        return tool_error(str(exc))
    if resp.status_code == 404:
        return tool_error(f"repo '{repo}' not found or outside agent's scope")
    if resp.status_code != 200:
        return tool_error(f"github API {resp.status_code}: {resp.text[:200]}")
    r = resp.json()
    return {
        "ok": True,
        "full_name": r["full_name"],
        "private": r.get("private"),
        "description": r.get("description"),
        "default_branch": r.get("default_branch"),
        "created_at": r.get("created_at"),
        "updated_at": r.get("updated_at"),
        "html_url": r.get("html_url"),
    }


def github_repo_delete(repo: str) -> Dict[str, Any]:
    err = _validate_repo(repo)
    if err:
        return tool_error(err)
    try:
        resp = _api("DELETE", f"/repos/{repo}")
    except RuntimeError as exc:
        return tool_error(str(exc))
    if resp.status_code == 204:
        return {"ok": True, "deleted": repo}
    if resp.status_code == 403:
        return tool_error(
            f"forbidden — current credentials lack delete permission on '{repo}'"
        )
    if resp.status_code == 404:
        return tool_error(f"repo '{repo}' not found or outside agent's scope")
    return tool_error(f"github API {resp.status_code}: {resp.text[:200]}")


def github_repo_create(
    name: str,
    org: Optional[str] = None,
    private: bool = True,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    if not name or not _NAME_RE.match(name):
        return tool_error(f"invalid name '{name}' — expected [A-Za-z0-9_.-]+")
    body: Dict[str, Any] = {"name": name, "private": bool(private)}
    if description:
        body["description"] = description
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    try:
        resp = _api("POST", path, json_body=body)
    except RuntimeError as exc:
        return tool_error(str(exc))
    if resp.status_code == 201:
        r = resp.json()
        return {"ok": True, "full_name": r["full_name"], "html_url": r["html_url"]}
    return tool_error(f"github API {resp.status_code}: {resp.text[:300]}")


# ---------- Schemas ----------------------------------------------------------


GITHUB_REPO_LIST_SCHEMA = {
    "name": "github_repo_list",
    "description": (
        "List GitHub repositories accessible to this agent. Pass `org` to "
        "filter to a single org; omit to see all repos in scope. Returns up "
        "to `limit` repos (default 100, max 100)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "org": {
                "type": "string",
                "description": "Organization name; omit to list all accessible repos.",
            },
            "limit": {
                "type": "integer",
                "description": "Max repos to return (1..100, default 100).",
                "default": 100,
            },
        },
        "additionalProperties": False,
    },
}

GITHUB_REPO_VIEW_SCHEMA = {
    "name": "github_repo_view",
    "description": (
        "Show details for a GitHub repository. `repo` is the full name "
        "like 'owner/name'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Full repo name like 'owner/name'.",
            },
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
}

GITHUB_REPO_DELETE_SCHEMA = {
    "name": "github_repo_delete",
    "description": (
        "Delete a GitHub repository. IRREVERSIBLE. Use only when explicitly "
        "requested or cleaning up known-stale workspaces. `repo` is full "
        "name like 'owner/name'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {
                "type": "string",
                "description": "Full repo name like 'owner/name'.",
            },
        },
        "required": ["repo"],
        "additionalProperties": False,
    },
}

GITHUB_REPO_CREATE_SCHEMA = {
    "name": "github_repo_create",
    "description": (
        "Create a new GitHub repository. Defaults to private. Omit `org` to "
        "create under the authenticated user / app."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Repository name (no owner prefix).",
            },
            "org": {
                "type": "string",
                "description": "Organization to create under; omit for user account.",
            },
            "private": {
                "type": "boolean",
                "description": "Private repo (default true).",
                "default": True,
            },
            "description": {
                "type": "string",
                "description": "Optional short description.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


# ---------- Registration -----------------------------------------------------

TOOL_NAMES = (
    "github_repo_list",
    "github_repo_view",
    "github_repo_delete",
    "github_repo_create",
)


def register_tools() -> None:
    """Register handlers + schemas with the in-image tools.registry.

    Idempotent — safe to call multiple times during plugin reload.
    """
    registry.register(
        name="github_repo_list",
        toolset="github",
        schema=GITHUB_REPO_LIST_SCHEMA,
        handler=lambda args, **kw: github_repo_list(
            org=args.get("org"), limit=args.get("limit", 100)
        ),
        check_fn=_credentials_available,
        description=GITHUB_REPO_LIST_SCHEMA["description"],
        emoji="📋",
    )
    registry.register(
        name="github_repo_view",
        toolset="github",
        schema=GITHUB_REPO_VIEW_SCHEMA,
        handler=lambda args, **kw: github_repo_view(repo=args.get("repo", "")),
        check_fn=_credentials_available,
        description=GITHUB_REPO_VIEW_SCHEMA["description"],
        emoji="🔍",
    )
    registry.register(
        name="github_repo_delete",
        toolset="github",
        schema=GITHUB_REPO_DELETE_SCHEMA,
        handler=lambda args, **kw: github_repo_delete(repo=args.get("repo", "")),
        check_fn=_credentials_available,
        description=GITHUB_REPO_DELETE_SCHEMA["description"],
        emoji="🗑️",
    )
    registry.register(
        name="github_repo_create",
        toolset="github",
        schema=GITHUB_REPO_CREATE_SCHEMA,
        handler=lambda args, **kw: github_repo_create(
            name=args.get("name", ""),
            org=args.get("org"),
            private=args.get("private", True),
            description=args.get("description"),
        ),
        check_fn=_credentials_available,
        description=GITHUB_REPO_CREATE_SCHEMA["description"],
        emoji="➕",
    )
