"""Task lifecycle management: queue moves and review summaries."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import Config
from ..core.enums import TaskStatus
from ..core.gitops import ensure_branch


class TaskLifecycle:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log

    def task_dir(self, task_id: str, where: str = "active") -> Path:
        return self.cfg.queue_dir / where / task_id

    def intake(self, task) -> Path:
        td = self.task_dir(task.id)
        (td / "artifacts" / "progress").mkdir(parents=True, exist_ok=True)
        (td / "prompts").mkdir(exist_ok=True)
        (td / "original.md").write_text(task.body)
        (td / "task.json").write_text(_json({
            "id": task.id,
            "status": "active",
            "source": task.source,
            "created": _now(),
            "stage": "spec",
            "history": [],
        }))
        return td

    def park(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "parked" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "PARKED", reason, "parked")
        self.log(f"  task {task_id} PARKED: {reason}")

    def fail(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "failed" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "KICKED OUT", reason, "failed")
        self.log(f"  task {task_id} FAILED: {reason}")

    def complete(self, task_id: str, summary: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "done" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "DONE", summary, "done")
        self.log(f"  task {task_id} DONE")

    def _exec_summary(self, task_id: str, status: str, text: str, where: str) -> None:
        td = self.cfg.queue_dir / where / task_id
        original = (td / "original.md").read_text() if (td / "original.md").exists() else ""
        review_dir = self.cfg.queue_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{task_id}.md").write_text(f"""# Task: {task_id}

**Status:** {status}
**Date:** {_now()}

## Original requirement

{original}

## Executive summary

{text}

## Artifacts

- spec: `{td}/artifacts/spec.md`
- slices: `{td}/artifacts/slices.md`
- session outputs: `{td}/artifacts/*.out`
""")
        self.log(f"  exec summary: {review_dir / (task_id + '.md')}")

    def resolve_workdir(self, td: Path) -> Path:
        """If the task references an existing git repo, work there; else the task dir."""
        for m in re.findall(r"/[a-zA-Z0-9_./-]+", (td / "original.md").read_text()):
            p = Path(m)
            if p.is_dir() and (p / ".git").exists():
                return p
        return td


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(obj) -> str:
    import json
    return json.dumps(obj, indent=2)