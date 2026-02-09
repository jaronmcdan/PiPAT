"""Build / revision information.

This module answers: "what exact build is currently running?".

It intentionally does **no work at import time** so importing ROI stays cheap.
All discovery work is performed lazily and cached.

Revision sources (highest priority first):
1) Environment variables (useful for CI / containers):
   - ROI_REVISION
   - ROI_GIT_COMMIT
   - GIT_COMMIT
   - GITHUB_SHA
   - CI_COMMIT_SHA
   - SOURCE_VERSION
2) A generated module `roi._revision` (useful when `.git/` is not deployed).
3) `git rev-parse` when running from a git checkout.

Notes
- Many deployment flows (including this repo's `scripts/pi_install.sh`) copy the
  source tree **without** the `.git` directory. In that case, generating
  `roi/_revision.py` during install is the most reliable option.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional


_ENV_REV_KEYS: tuple[str, ...] = (
    "ROI_REVISION",
    "ROI_GIT_COMMIT",
    "GIT_COMMIT",
    "GITHUB_SHA",
    "CI_COMMIT_SHA",
    "SOURCE_VERSION",
)


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = os.getenv(k)
        if v is None:
            continue
        v = v.strip()
        if v:
            return v
    return None


def _shorten_sha(sha: str, n: int = 7) -> str:
    s = (sha or "").strip()
    if not s:
        return ""
    return s[:n]


def _find_git_root(start: Path, max_hops: int = 10) -> Optional[Path]:
    """Walk parents until we find a .git directory/file."""

    p = start.resolve()
    if p.is_file():
        p = p.parent

    for _ in range(max_hops):
        git_marker = p / ".git"
        if git_marker.exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        out = subprocess.check_output(
            args,
            cwd=str(cwd),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        out = (out or "").strip()
        return out or None
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_revision_full() -> str:
    """Return a full git SHA if available, otherwise ""."""

    # 1) Environment
    env = _first_env(_ENV_REV_KEYS)
    if env:
        return env

    # 2) Generated module (created by install scripts / CI)
    try:
        from ._revision import GIT_COMMIT as gen_sha  # type: ignore

        gen_sha = str(gen_sha or "").strip()
        if gen_sha:
            return gen_sha
    except Exception:
        pass

    # 3) Git checkout
    try:
        root = _find_git_root(Path(__file__))
        if root is None:
            # Fallback: try current working directory
            root = _find_git_root(Path.cwd())
        if root is None:
            return ""

        sha = _run_git(["git", "rev-parse", "HEAD"], cwd=root)
        return sha or ""
    except Exception:
        return ""


@lru_cache(maxsize=1)
def get_revision(short: bool = True) -> str:
    """Return the git commit hash (short by default) or "unknown"."""

    full = get_revision_full()
    if not full:
        return "unknown"
    return _shorten_sha(full) if short else full


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the installed package version, falling back to 0.0.0."""

    try:
        from . import __version__

        v = str(__version__ or "").strip()
        return v or "0.0.0"
    except Exception:
        return "0.0.0"


def build_banner(build_tag: Optional[str] = None) -> str:
    """Human-friendly single-line banner for logs."""

    ver = get_version()
    rev = get_revision(short=True)

    parts: list[str] = [f"ROI {ver}"]

    if rev != "unknown":
        parts.append(f"rev {rev}")

    if build_tag:
        tag = str(build_tag).strip()
        if tag and tag.lower() not in {"unknown"}:
            parts.append(f"tag {tag}")

    return " | ".join(parts)
