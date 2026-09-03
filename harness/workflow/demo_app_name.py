"""Derivation of a demo app's directory name from the issue title.

Demo spec §4: the app directory is `demo-apps/<app-name>/` with a short
kebab-case name derived from the issue title; on a collision with an
existing app directory the issue number is appended (edge case 8). A
title with no usable characters (emoji-only, empty) falls back to a fixed
name so every demo request still gets a directory.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

# Upper bound on the derived name; long titles are cut on a word-ish edge
# (trailing separators are stripped after the cut).
MAX_NAME_LENGTH = 48

# Name for a title that yields no usable characters at all.
FALLBACK_APP_NAME = "app"


def derive_app_name(title: str) -> str:
    """The kebab-case app name for an issue title (no collision check)."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").strip().lower())
    slug = slug.strip("-")[:MAX_NAME_LENGTH].strip("-")
    return slug or FALLBACK_APP_NAME


def resolve_app_name(title: str, issue_number: int, apps_dir: Path) -> str:
    """The app name for one demo request, collision-suffixed (edge case 8).

    `apps_dir` is the repository's apps directory (`demo-apps`); a name
    already taken by another app gets `-<issue-number>` appended, which is
    unique per issue.
    """
    base = derive_app_name(title)
    if (Path(apps_dir) / base).exists():
        return f"{base}-{issue_number}"
    return base


def resolve_generation_app_name(title: str, issue_number: int,
                                apps_dir: Path,
                                owner_of: Callable[[Path], int | None]) -> str:
    """The app name for the FINAL generation, reusing this issue's
    placeholder directory (FR-2.4) and suffixing only on a real
    collision (edge case 8).

    The placeholder was written under `resolve_app_name`'s verdict, so
    this issue's directory is either the bare name or `<base>-<issue>`:

      * `<base>-<issue>` exists — that is our placeholder (the suffix is
        unique per issue); generate there, replacing the placeholder;
      * the bare name exists but is stamped to a different issue (or
        unstamped — an existing app we know nothing about) — suffix, so
        the other app is never clobbered;
      * otherwise the bare name is free or is our own placeholder — use
        it, so the final app replaces the placeholder in place.

    `owner_of` reads an app directory's owning issue number (see
    `demo_placeholder.placeholder_issue_of`); it is injected so this
    module stays free of the placeholder's page format.
    """
    base = derive_app_name(title)
    suffixed = f"{base}-{issue_number}"
    if (Path(apps_dir) / suffixed).exists():
        return suffixed
    bare = Path(apps_dir) / base
    if bare.exists() and owner_of(bare) != issue_number:
        return suffixed
    return base
