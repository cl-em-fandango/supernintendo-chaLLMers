"""Task provider adapters.

A TaskProvider is the single integration point for where tasks come from.
The pipeline only ever talks to this interface, so new sources (GitHub
issues, a database, an API, ...) can be added without touching the pipeline.

Interface:
    fetch_pending() -> list[Task]     # tasks ready to work on
    submit(task: Task) -> None        # (optional) push results back upstream

Task is a plain dataclass: id + freeform markdown body + optional source hint.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Task:
    id: str
    body: str
    source: str = ""            # e.g. "directory", "github:owner/repo#123"
    meta: dict = field(default_factory=dict)


class TaskProvider(ABC):
    """Adapter interface for task sources."""

    name: str = "abstract"

    @abstractmethod
    def fetch_pending(self) -> list[Task]:
        """Return tasks ready to be worked on, in priority order."""

    def submit(self, task: Task, status: str, summary: str) -> None:
        """Optional: report a finished task back to the source. No-op by default."""


class DirectoryTaskProvider(TaskProvider):
    """Tasks are markdown files dropped into a pending directory.

    One file = one task. Filename (sans .md) becomes the task id.

    Lifecycle is file-based: a task is CLAIMED by moving pending/X.md into
    active/ at fetch time, so a parked/failed/done task can never be
    re-claimed. The pipeline then moves the active/ dir to its terminal
    folder. To retry a parked task, move it back to pending/ (see `unpark`).
    """

    name = "directory"

    def __init__(self, pending_dir: str | Path, claimed_dir: str | Path | None = None):
        self.pending_dir = Path(pending_dir)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        # Claimed files are staged here (out of pending/) so they are not
        # re-fetched. The pipeline reads the body at claim time, then the
        # staging file is removed once intake has copied it into the task dir.
        self.claimed_dir = Path(claimed_dir) if claimed_dir else self.pending_dir.parent / "claimed"
        self.claimed_dir.mkdir(parents=True, exist_ok=True)

    def fetch_pending(self, claim: bool = False) -> list[Task]:
        """List pending tasks. With claim=True, move each pending/X.md into
        claimed/ so it is not seen again until explicitly requeued."""
        tasks = []
        for f in sorted(self.pending_dir.glob("*.md")):
            tid = _slug(f.stem)
            body = f.read_text()
            if claim:
                dest = self.claimed_dir / f.name
                try:
                    f.rename(dest)
                except OSError:
                    # already claimed by a concurrent run; skip
                    continue
            tasks.append(Task(id=tid, body=body, source=f"directory:{f.name}"))
        return tasks

    def release_claim(self, task: Task) -> None:
        """Remove the staging file once intake has persisted the body."""
        for f in self.claimed_dir.glob("*.md"):
            if _slug(f.stem) == task.id:
                f.unlink(missing_ok=True)
                break

    def submit(self, task: Task, status: str, summary: str) -> None:
        # Directory provider lifecycle is managed by the pipeline moving files
        # between queue subdirectories; nothing to push back upstream.
        pass


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")
    return s[:60] or "task"


def create_provider(cfg) -> TaskProvider:
    """Factory: build the configured provider. Add new adapters here."""
    kind = cfg.task_provider
    if kind == "directory":
        pending = cfg.directory_provider.get(
            "pendingDir", str(cfg.queue_dir / "pending"))
        claimed = cfg.directory_provider.get(
            "claimedDir", str(cfg.queue_dir / "claimed"))
        return DirectoryTaskProvider(pending, claimed)
    raise ValueError(f"unknown task provider: {kind!r}")
