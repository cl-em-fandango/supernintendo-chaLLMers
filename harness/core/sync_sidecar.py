"""Sidecar records linking a queue task to its GitHub issue (spec FR-1.6).

When the inbound sync imports an issue, or the outbound sync creates one,
the linkage is recorded beside the task — never inside the task markdown:

* a task *file* (`pending/X.md`, `claimed/X.md`, ...) gets `X.md.gh.json`
  next to it;
* an active/terminal *task directory* gets `gh.json` inside it.

`SyncLinkage` is the shape of one record; the functions here read and write
it. A missing or corrupt sidecar reads as `None` — the task is then treated
as unlinked and title matching (FR-2.1) is the fallback. Sidecar lookups
take precedence over title matching wherever the sync resolves a task
(FR-1.6). Writes go through `task_lifecycle.write_atomic` (spec §9), so a
crash mid-pass never leaves a half-written linkage.

`comment_ids` is reserved for the handoff-comment dedup map (FR-2.5, a
later slice); it round-trips untouched and defaults to empty.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..workflow.task_lifecycle import write_atomic

# `pending/X.md` -> `pending/X.md.gh.json` (beside the task file).
SIDECAR_SUFFIX = ".gh.json"
# An active task dir carries its own `gh.json`.
TASK_DIR_SIDECAR_NAME = "gh.json"


@dataclass
class SyncLinkage:
    """One task's GitHub linkage record: issue number, `owner/name` repo,
    and the handoff-comment dedup map (event id -> comment id)."""
    issue: int
    repo: str
    comment_ids: dict = field(default_factory=dict)


def file_sidecar_path(task_file: Path) -> Path:
    """The sidecar path for a task *file* (it need not exist yet)."""
    return task_file.with_name(task_file.name + SIDECAR_SUFFIX)


def task_dir_sidecar_path(task_dir: Path) -> Path:
    """The sidecar path inside an active/terminal task directory."""
    return task_dir / TASK_DIR_SIDECAR_NAME


def write_linkage(sidecar: Path, linkage: SyncLinkage) -> None:
    """Atomically write `linkage` to `sidecar` (FR-1.6)."""
    payload = {
        "issue": linkage.issue,
        "repo": linkage.repo,
        "comment_ids": dict(linkage.comment_ids),
    }
    write_atomic(sidecar, json.dumps(payload, indent=2) + "\n")


def move_sidecar_into_task_dir(sidecar: Path, task_dir: Path) -> None:
    """Relocate a task-file sidecar into the task directory the task moved
    to (FR-1.6): `pending/X.md.gh.json` -> `parked/X/gh.json`.

    A sidecar with no readable linkage (missing, corrupt, or not ours —
    e.g. a claim-ownership file) leaves nothing of ours to move."""
    linkage = read_linkage(sidecar)
    if linkage is None:
        return
    write_linkage(task_dir_sidecar_path(task_dir), linkage)
    sidecar.unlink(missing_ok=True)


def read_linkage(sidecar: Path) -> SyncLinkage | None:
    """Read one sidecar; None when it is missing, unreadable or corrupt.

    A corrupt linkage must not crash the sync (NFR-1) — the task simply
    reads as unlinked and the title-match fallback applies.
    """
    try:
        raw = json.loads(sidecar.read_text())
        return SyncLinkage(
            issue=int(raw["issue"]),
            repo=str(raw.get("repo", "")),
            comment_ids=dict(raw.get("comment_ids") or {}),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
