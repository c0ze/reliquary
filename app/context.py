"""Caller-context resolution for Reliquary's optional, soft governance layer.

Dependency-light: stdlib + catalog.slugify_value. No Mem0/server imports.

When a coding agent supplies project context (a nested ``context`` arg, or the
X-Reliquary-* headers), Reliquary can softly bias retrieval toward that repo's
memory. Absent context => generic behavior, unchanged. ``cwd``/``git_root`` are
opaque informational strings: Reliquary never touches the filesystem at those paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from catalog import slugify_value

_REPO_HEADER = "x-reliquary-repo"
_GIT_ROOT_HEADER = "x-reliquary-git-root"
_CWD_HEADER = "x-reliquary-cwd"
_CLIENT_HEADER = "x-reliquary-client"


@dataclass
class CallerContext:
    client: str | None = None
    cwd: str | None = None
    git_root: str | None = None
    repo: str | None = None
    repo_slug: str | None = None


def _derive_slug(repo: str | None, git_root: str | None) -> str | None:
    if repo:
        slug = slugify_value(repo.rstrip("/").split("/")[-1])
        if slug:
            return slug
    if git_root:
        slug = slugify_value(os.path.basename(git_root.rstrip("/")))
        if slug:
            return slug
    return None


def resolve_context(arguments: dict | None, headers: dict | None) -> CallerContext | None:
    arguments = arguments if isinstance(arguments, dict) else {}
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    raw = arguments.get("context")
    if not isinstance(raw, dict):
        raw = {}

    def pick(key: str, header: str) -> str | None:
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        hval = headers.get(header)
        if isinstance(hval, str) and hval.strip():
            return hval.strip()
        return None

    repo = pick("repo", _REPO_HEADER)
    git_root = pick("git_root", _GIT_ROOT_HEADER)
    repo_slug = _derive_slug(repo, git_root)
    if repo_slug is None:
        return None  # no usable project signal => generic behavior
    return CallerContext(
        client=pick("client", _CLIENT_HEADER),
        cwd=pick("cwd", _CWD_HEADER),
        git_root=git_root,
        repo=repo,
        repo_slug=repo_slug,
    )
