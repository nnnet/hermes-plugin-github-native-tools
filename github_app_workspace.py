"""GitHub-backed kanban workspace integration.

Why
---
When the operator configures a GitHub App (env vars ``GITHUB_APP_ID``,
``GITHUB_APP_INSTALLATION_ID``, ``GITHUB_APP_PRIVATE_KEY_PATH``), each
kanban *board* should be mirrored as a private GitHub repository so that
every task workspace under that board gets persistent off-host backup +
the operator can review agent artefacts via the GitHub UI.

The default is *plain dirs* (current behaviour). This module is purely
additive: when App creds are missing OR any git/network step fails, we
fall back to the plain workspace and log a warning. Nothing inside the
hermes runtime should error out because a workspace push failed.

Repository naming
-----------------
``<prefix><board-slug>`` where prefix comes from
``HERMES_WORKSPACE_REPO_PREFIX`` (default ``TEST-``). Examples:

    board ``smoke-tests``   → ``TEST-smoke-tests``
    board ``research-prod`` → ``TEST-research-prod``

The repo is created under the GitHub org the App is installed in
(``HERMES_GITHUB_ORG`` env var, default looked up dynamically from the
App's installations endpoint).

Push timing
-----------
* On workspace creation (``resolve_workspace``): ``git init`` + initial
  empty commit + push.
* On task complete (``kanban_complete`` hook): ``git add -A && git commit
  && git push``.

Intermediate file writes are NOT auto-pushed — that would saturate the
API quota and produce a noisy commit history. Operators who want
finer-grained tracking can call ``commit_and_push`` explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Env knobs (read at call-time, not import-time, so test/fixture overrides win)
_PREFIX_ENV = "HERMES_WORKSPACE_REPO_PREFIX"
_PREFIX_DEFAULT = "TEST-"
_ORG_ENV = "HERMES_GITHUB_ORG"

# Slug pattern: lowercase alphanumeric + hyphens, no leading/trailing hyphen.
# GitHub repo names allow [A-Za-z0-9._-] but we normalise to a tight subset
# so the same board slug always produces the same repo name.
_SLUG_BAD = re.compile(r"[^a-z0-9-]+")


# Whitelist .gitignore for kanban workspaces. Kanban tasks often
# download/produce large binary artefacts (audio from YouTube ingest,
# video frames, model weights, vector DB snapshots, sqlite caches);
# pushing those over GitHub's 100 MB per-file hard limit blocks the
# entire commit and stalls the whole board mirror.
#
# Strategy: exclude EVERYTHING by default, then un-ignore the small
# text-y artefact types worth keeping in git history (markdown notes,
# code, configs, structured data). Workers that genuinely need a big
# binary tracked can ``git add -f <path>`` once, or extend the per-task
# ``.gitignore`` in their workspace.
_DEFAULT_GITIGNORE = """# Hermes workspace .gitignore — auto-managed by
# tools/github_app_workspace.py. Re-rendered on every workspace init.
#
# Whitelist mode: everything is ignored by default; only the file types
# below are tracked. This keeps audio/video/model-weight blobs out of
# the GitHub mirror without per-task curation. To track an exotic file
# explicitly: ``git add -f path/to/file`` once.

# Ignore EVERYTHING by default ...
*

# ... then opt-in to text-y artefact types worth preserving.
!*/
!.gitignore
!*.md
!*.markdown
!*.txt
!*.rst
!*.json
!*.jsonl
!*.yaml
!*.yml
!*.toml
!*.ini
!*.cfg
!*.csv
!*.tsv
!*.py
!*.js
!*.ts
!*.tsx
!*.jsx
!*.html
!*.css
!*.sh
!*.bash
!*.sql
!*.diff
!*.patch
!*.log.gz
!Dockerfile
!Makefile
!README*
!LICENSE*

# Always exclude — these slip through the whitelist on extension but
# are universally noise we never want.
.cache/
__pycache__/
.venv/
node_modules/
*.pyc
.DS_Store
"""


def is_configured() -> bool:
    """Return True when all three GitHub App env vars are present.

    Why: caller (``resolve_workspace``) needs a single cheap probe to
    decide whether to attempt the git path. We don't actually fetch a
    token here — that happens lazily inside ``ensure_remote_repo``.
    """
    return bool(
        os.environ.get("GITHUB_APP_ID")
        and os.environ.get("GITHUB_APP_INSTALLATION_ID")
        and os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    )


def repo_name_for_board(board_slug: str) -> str:
    """Compute the GitHub repository name for a kanban board.

    Normalises the board slug to ``[a-z0-9-]+`` and prepends the
    operator's configured prefix.
    """
    prefix = os.environ.get(_PREFIX_ENV, _PREFIX_DEFAULT)
    cleaned = _SLUG_BAD.sub("-", (board_slug or "default").lower()).strip("-")
    return f"{prefix}{cleaned or 'default'}"


def _get_app_token() -> Optional[str]:
    """Return a fresh GitHub App installation token, or None on failure."""
    try:
        from tools.skills_hub import GitHubAuth
    except Exception:
        return None
    try:
        auth = GitHubAuth()
        tok = auth._try_github_app()  # noqa: SLF001 — explicit App path
        return tok or None
    except Exception as exc:
        logger.debug("github app token lookup failed: %s", exc)
        return None


def _build_app_jwt() -> Optional[str]:
    """Build an App-level JWT for endpoints under /app/...

    Mirrors the JWT step in ``GitHubAuth._try_github_app`` but returns the
    JWT itself instead of trading it for an installation token. Used by
    ``_get_org`` to query the App's installations (which requires
    App-level auth, not the installation token).
    """
    app_id = os.environ.get("GITHUB_APP_ID")
    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if not (app_id and key_path):
        return None
    try:
        import jwt  # PyJWT
    except ImportError:
        return None
    try:
        from pathlib import Path as _Path
        import time as _time
        key_file = _Path(key_path)
        if not key_file.exists():
            return None
        private_key = key_file.read_text(encoding="utf-8")
        now = int(_time.time())
        payload = {"iat": now - 60, "exp": now + (10 * 60), "iss": app_id}
        return jwt.encode(payload, private_key, algorithm="RS256")
    except Exception as exc:
        logger.debug("_build_app_jwt failed: %s", exc)
        return None


def _get_org(token: str) -> Optional[str]:
    """Resolve the org slug under which to create workspace repos.

    Priority:
      1. ``HERMES_GITHUB_ORG`` env (explicit override)
      2. The GitHub App's installation account (looked up via API)

    ``token`` is the installation token (unused at this layer — the
    ``/app/installations/<id>`` endpoint requires the App-level JWT
    instead, which we build inline).
    """
    explicit = (os.environ.get(_ORG_ENV) or "").strip()
    if explicit:
        return explicit
    inst_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
    if not inst_id:
        return None
    app_jwt = _build_app_jwt()
    if not app_jwt:
        return None
    try:
        resp = httpx.get(
            f"https://api.github.com/app/installations/{inst_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            account = (resp.json().get("account") or {})
            login = account.get("login")
            if login:
                return login
        else:
            logger.debug(
                "installation lookup returned %s: %s",
                resp.status_code, resp.text[:200],
            )
    except Exception as exc:
        logger.debug("installation org lookup failed: %s", exc)
    return None


def ensure_remote_repo(
    board_slug: str, *, private: bool = True
) -> Optional[Tuple[str, str]]:
    """Make sure a GitHub repo exists for the given board.

    Returns ``(clone_url_with_token, token)`` on success — the URL embeds
    the installation token so ``git push`` works without prompting.
    Returns ``None`` if creation failed or the App isn't configured.
    """
    if not is_configured():
        return None
    token = _get_app_token()
    if not token:
        logger.info("github_app_workspace: no App token available")
        return None
    org = _get_org(token)
    if not org:
        logger.warning("github_app_workspace: org could not be resolved")
        return None

    name = repo_name_for_board(board_slug)
    base_headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Check if the repo already exists — saves an API call + avoids 422
    # when re-resolving an existing workspace.
    try:
        check = httpx.get(
            f"https://api.github.com/repos/{org}/{name}",
            headers=base_headers,
            timeout=10,
        )
    except Exception as exc:
        logger.warning("github_app_workspace: repo HEAD failed: %s", exc)
        return None

    if check.status_code == 200:
        clone_url = f"https://x-access-token:{token}@github.com/{org}/{name}.git"
        return clone_url, token

    if check.status_code != 404:
        logger.warning(
            "github_app_workspace: unexpected repo lookup status %s: %s",
            check.status_code, check.text[:200],
        )
        return None

    # 404 — create the repo. POST /orgs/{org}/repos creates under the org;
    # auto_init=false so we control the initial commit ourselves.
    try:
        create = httpx.post(
            f"https://api.github.com/orgs/{org}/repos",
            headers=base_headers,
            json={
                "name": name,
                "private": bool(private),
                "auto_init": False,
                "description": f"Hermes kanban workspace for board {board_slug}",
            },
            timeout=15,
        )
    except Exception as exc:
        logger.warning("github_app_workspace: repo create failed: %s", exc)
        return None

    if create.status_code not in (201, 202):
        logger.warning(
            "github_app_workspace: repo create returned %s: %s",
            create.status_code, create.text[:200],
        )
        return None

    clone_url = f"https://x-access-token:{token}@github.com/{org}/{name}.git"
    return clone_url, token


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


def _run_git(args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Thin git subprocess wrapper with sensible defaults.

    safe.directory=*: workspace dirs live on a host bind-mount under
    /opt/data/kanban/workspaces/. Even when host UID matches the
    container's hermes UID, git's recent "dubious ownership" guard
    refuses to operate inside the tree on first contact. The kanban
    workspace is intentionally writable by the agent — bypass the
    check unconditionally. Risk: zero (we control the dir layout).
    """
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env={
            **os.environ,
            # Keep agent's main env, but force these so the commit
            # identity is deterministic even when the container user has
            # no user.email/user.name set globally.
            "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "Hermes Bot"),
            "GIT_AUTHOR_EMAIL": os.environ.get(
                "GIT_AUTHOR_EMAIL", "hermes-bot@users.noreply.github.com"
            ),
            "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "Hermes Bot"),
            "GIT_COMMITTER_EMAIL": os.environ.get(
                "GIT_COMMITTER_EMAIL", "hermes-bot@users.noreply.github.com"
            ),
        },
    )


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def init_workspace_repo(path: Path, board_slug: str) -> bool:
    """Initialise ``path`` as a git repo backed by the board's GitHub repo.

    Idempotent — if the repo already has ``.git`` and a matching remote,
    nothing happens. Otherwise: ``git init`` → set ``origin`` → push an
    empty initial commit.

    Returns True on success (including no-op idempotent case), False on
    any failure (the caller falls back to plain-dir behaviour).
    """
    if not is_configured():
        return False

    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)

    remote = ensure_remote_repo(board_slug)
    if remote is None:
        return False
    clone_url, _token = remote

    try:
        if not _is_git_repo(path):
            _run_git(["init", "-b", "main"], cwd=path)
        # (Re)write .gitignore every init — operators may bump the list
        # between releases and we want existing boards to pick up the
        # tighter rules without manual intervention. The file is checked
        # in (under VCS), so changes to it will land as a regular commit.
        (path / ".gitignore").write_text(_DEFAULT_GITIGNORE, encoding="utf-8")

        # Set/refresh origin every time — the token rotates each hour, so
        # the URL embedded last time may be stale.
        existing = _run_git(["remote"], cwd=path, check=False).stdout.split()
        if "origin" in existing:
            _run_git(["remote", "set-url", "origin", clone_url], cwd=path)
        else:
            _run_git(["remote", "add", "origin", clone_url], cwd=path)

        # Initial commit if the repo has no commits yet.
        head = _run_git(
            ["rev-parse", "--verify", "HEAD"], cwd=path, check=False
        )
        if head.returncode != 0:
            _run_git(["add", "-A"], cwd=path, check=False)
            _run_git(
                ["commit", "--allow-empty", "-m", f"chore: init workspace for board {board_slug}"],
                cwd=path,
            )
            _run_git(["push", "-u", "origin", "main"], cwd=path)
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "github_app_workspace: init/push for board %s failed (exit=%s): %s",
            board_slug, exc.returncode, (exc.stderr or "")[:300],
        )
        return False
    except Exception as exc:
        logger.warning(
            "github_app_workspace: init/push for board %s raised: %s",
            board_slug, exc,
        )
        return False


def commit_and_push(path: Path, message: str, *, board_slug: Optional[str] = None) -> bool:
    """Stage all changes under ``path``, commit, and push to origin.

    Skips silently when:
      * App not configured, OR
      * ``path`` is not inside a git repo, OR
      * there are no staged changes (still tries to push the existing HEAD).

    Returns True on a successful push (or no-op when nothing changed),
    False on any error. Never raises.
    """
    if not is_configured():
        return False

    path = path.expanduser().resolve()
    if not _is_git_repo(path) and not _find_git_root(path):
        return False

    # If we have the board slug, refresh remote URL so the latest App token
    # is embedded (tokens rotate hourly). For commit-and-push from inside a
    # task subdirectory we still want the repo root to receive the push.
    repo_root = _find_git_root(path) or path
    try:
        if board_slug:
            remote = ensure_remote_repo(board_slug)
            if remote is None:
                return False
            clone_url, _token = remote
            _run_git(["remote", "set-url", "origin", clone_url], cwd=repo_root, check=False)

        _run_git(["add", "-A"], cwd=repo_root)
        status = _run_git(["status", "--porcelain"], cwd=repo_root, check=False)
        if status.stdout.strip():
            _run_git(["commit", "-m", message], cwd=repo_root)
        # Push even when there are no new commits — keeps a deterministic
        # exit code for callers and surfaces remote-side problems early.
        _run_git(["push", "origin", "HEAD:main"], cwd=repo_root)
        return True
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "github_app_workspace: commit_and_push at %s failed (exit=%s): %s",
            repo_root, exc.returncode, (exc.stderr or "")[:300],
        )
        return False
    except Exception as exc:
        logger.warning("github_app_workspace: commit_and_push raised: %s", exc)
        return False


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk upward from ``start`` and return the nearest dir containing ``.git``."""
    cur = start
    for _ in range(10):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


__all__ = [
    "is_configured",
    "repo_name_for_board",
    "ensure_remote_repo",
    "init_workspace_repo",
    "commit_and_push",
]
