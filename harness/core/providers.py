"""Task provider adapters.

A TaskProvider is the single integration point for where tasks come from.
The pipeline only ever talks to this interface, so new sources (GitHub
issues, a database, an API, ...) can be added without touching the pipeline.

Interface:
    fetch_pending() -> list[Task]     # tasks ready to work on
    list_claims() -> list[Task]       # tasks held claimed but not yet processed
    requeue_all_claims() -> list[str] # hand every claim back to the pending pool
    submit(task: Task) -> None        # (optional) push results back upstream

A claim is a fetch side effect, so recovering from one is the provider's job:
sources with no claim concept inherit empty defaults and stay valid adapters.

Task is a plain dataclass: id + freeform markdown body + optional source hint.
"""
from __future__ import annotations

import re
import time
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

    def list_claims(self) -> list[Task]:
        """Tasks currently claimed but not yet processed. Sources without a
        claim lifecycle have none, so the default is empty."""
        return []

    def requeue_claim(self, name_or_task: "str | Task") -> str | None:
        """Hand one claim back to the pending pool. Returns the new path, or
        None when this provider cannot requeue (no claim lifecycle)."""
        return None

    def requeue_all_claims(self) -> list[str]:
        """Hand every claim back to the pending pool. Returns the moved names."""
        return []

    def claim_age_hours(self, name: str) -> float:
        """Age in hours of a claim; -1.0 when there is no such claim."""
        return -1.0

    def submit(self, task: Task, status: str, summary: str) -> None:
        """Optional: report a finished task back to the source. No-op by default."""


class DirectoryTaskProvider(TaskProvider):
    """Tasks are markdown files dropped into a pending directory.

    One file = one task. Filename (sans .md) becomes the task id.

    Lifecycle is file-based: a task is CLAIMED by moving pending/X.md into
    active/ at fetch time, so a parked/failed/done task can never be
    re-claimed. The pipeline then moves the active/ dir to its terminal
    folder. To retry a parked task, move it back to pending/ (see `unpark`).

    A claim is this provider's own side effect, so claim recovery lives here
    too: `list_claims()`, `requeue_claim()`, `requeue_all_claims()` and
    `claim_age_hours()` see and undo claims without touching the queue layout.
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

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None) -> list[Task]:
        """List pending tasks. With claim=True, move each pending/X.md into
        claimed/ so it is not seen again until explicitly requeued.

        `limit` caps how many tasks are returned, so a caller that asks for
        one claim gets one instead of the whole queue; whatever it did not
        take stays in pending/. Default None is every task, as before.
        """
        tasks = []
        for f in sorted(self.pending_dir.glob("*.md")):
            if limit is not None and len(tasks) >= limit:
                break
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

    def list_claims(self) -> list[Task]:
        """The files sitting in claimed/, in sorted order, as tasks."""
        return [Task(id=_slug(f.stem), body=f.read_text(), source=f"claimed:{f.name}")
                for f in sorted(self.claimed_dir.glob("*.md"))]

    def requeue_claim(self, name_or_task: "str | Task") -> str | None:
        """Move one claimed file back to pending/, by filename or Task.

        Returns the new path as a string, or None when there is no such claim.
        A pending/ name collision gets a `-requeued` suffix; nothing is
        ever overwritten.
        """
        src = self._claim_path(name_or_task)
        if src is None:
            return None
        return self._move_to_pending(src)

    def requeue_all_claims(self) -> list[str]:
        """Move every claimed file back to pending/. Returns the moved names."""
        moved = []
        for f in sorted(self.claimed_dir.glob("*.md")):
            if self._move_to_pending(f) is not None:
                moved.append(f.name)
        return moved

    def claim_age_hours(self, name: str) -> float:
        """Hours since a claimed file was last written; -1.0 if not claimed.

        A file that has already been handed back keeps its claim mtime, so
        the pending copy is aged too — a name only reads as absent (-1.0) when
        it is in neither claimed/ nor pending/.
        """
        src = _named_file(self.claimed_dir, name) or _named_file(self.pending_dir, name)
        if src is None:
            return -1.0
        return (time.time() - src.stat().st_mtime) / 3600.0

    def _claim_path(self, name_or_task: "str | Task") -> Path | None:
        """Resolve a filename, a bare stem, or a Task to its claimed file."""
        if isinstance(name_or_task, Task):
            return next((f for f in sorted(self.claimed_dir.glob("*.md"))
                         if _slug(f.stem) == name_or_task.id), None)
        return _named_file(self.claimed_dir, name_or_task)

    def _move_to_pending(self, src: Path) -> str | None:
        """Move one claimed file into pending/, never overwriting. Path str."""
        dest = self.pending_dir / src.name
        n = 0
        while dest.exists():
            n += 1
            tag = "-requeued" if n == 1 else f"-requeued-{n - 1}"
            dest = self.pending_dir / f"{src.stem}{tag}.md"
        try:
            src.rename(dest)
        except OSError:
            # vanished or held elsewhere: leave it claimed rather than lose it
            return None
        return str(dest)

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


def _named_file(directory: Path, name: str) -> Path | None:
    """`directory/<name>.md` if it exists; `name` may already carry the .md."""
    filename = Path(str(name)).name
    candidate = directory / (filename if filename.endswith(".md") else f"{filename}.md")
    return candidate if candidate.is_file() else None


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
