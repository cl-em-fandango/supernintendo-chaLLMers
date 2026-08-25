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
    """

    name = "directory"

    def __init__(self, pending_dir: str | Path):
        self.pending_dir = Path(pending_dir)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def fetch_pending(self) -> list[Task]:
        tasks = []
        for f in sorted(self.pending_dir.glob("*.md")):
            tasks.append(Task(
                id=_slug(f.stem),
                body=f.read_text(),
                source=f"directory:{f.name}",
            ))
        return tasks

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
        return DirectoryTaskProvider(pending)
    raise ValueError(f"unknown task provider: {kind!r}")
