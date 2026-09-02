"""One task's GitHub linkage: the `github` section of its metadata record.

The linkage says which issue a task represents, in which repo, which handoff
comments were already mirrored onto it, and whether the request came in as a
demo. It is the shape both stores hold — the task record
(`task_record.py`, `<queue>/.meta/<task-id>.json`) and, until a queue is
migrated, the legacy sidecar files the migration reader folds in.

A missing or unreadable linkage reads as `None` at every layer: the task is
unlinked, and title matching (sync spec FR-2.1) is the caller's fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SyncLinkage:
    """One task's GitHub linkage record: issue number, `owner/name` repo,
    the handoff-comment dedup map (event id -> comment id), and the demo
    flag (demo spec FR-1.3: the issue carried `snes-demo` at ingest)."""
    issue: int
    repo: str
    comment_ids: dict = field(default_factory=dict)
    demo: bool = False
