"""Autonomous mode: generate tasks until the queue has N.

Loop:
  1. random model A analyzes the codebase and proposes a feature
  2. random model B (different) reviews the proposal
  3. pass -> write to pending queue; reject -> try again
Stop when pending queue has >= target tasks.
"""
from __future__ import annotations

import random
import re
import shutil
from pathlib import Path

from ..core import prompts
from ..core.config import Config
from ..core.providers import Task
from ..core.session import SessionRunner


class AutonomousGenerator:
    def __init__(self, cfg: Config, runner: SessionRunner, provider, log=print):
        self.cfg = cfg
        self.runner = runner
        self.provider = provider
        self.log = log

    def _random_model(self, exclude: str | None = None) -> str:
        # Autonomous suggest/review is high-volume and low-stakes -> use fast models.
        pool = [m for m in self.cfg.fast_pool if m != exclude]
        if not pool:
            pool = [m for m in self.cfg.random_pool if m != exclude]
        return random.choice(pool) if pool else self.cfg.random_pool[0]

    def run(self, workdir: Path) -> int:
        target = self.cfg.autonomous_queue_target
        added = 0
        attempts = 0
        max_attempts = target * 6  # safety valve

        while self._pending_count() < target and attempts < max_attempts:
            attempts += 1
            self.log(f"── autonomous attempt {attempts} "
                     f"(pending={self._pending_count()}/{target}) ──")

            ma = self._random_model()
            r = self.runner.run(
                ma, workdir, prompts.autonomous_suggest(),
                stage="autonomous_suggest", notes="proposal")
            if r.verdict != "done":
                self.log(f"  suggestion failed (verdict={r.verdict}); retrying")
                continue

            proposal = _proposal_text(r.output)
            if not proposal.strip():
                self.log("  empty proposal; retrying")
                continue

            prop_file = self.cfg.work_dir / "logs" / f"autonomous-proposal-{attempts}.md"
            prop_file.parent.mkdir(parents=True, exist_ok=True)
            prop_file.write_text(proposal)

            mb = self._random_model(exclude=ma)
            r = self.runner.run(
                mb, workdir, prompts.autonomous_review(prop_file),
                stage="autonomous_review", notes=f"review of {ma}'s proposal")
            self.log(f"  review ({mb}) verdict: {r.verdict}")
            if r.verdict != "pass":
                self.log("  proposal rejected; trying again")
                continue

            task_id = _task_id(proposal, attempts)
            dest = self.cfg.queue_dir / "pending" / f"{task_id}.md"
            dest.write_text(proposal)
            self.log(f"  ➕ queued task: {task_id}")
            added += 1

        self.log(f"autonomous mode finished: added {added} tasks "
                 f"({self._pending_count()} pending)")
        return added

    def _pending_count(self) -> int:
        return len(self.provider.fetch_pending())


def _proposal_text(output: str) -> str:
    """Extract the proposal body (everything before the Summary/VERDICT tail)."""
    m = re.search(r"## Summary\n", output)
    body = output[:m.start()] if m else output
    body = re.sub(r"VERDICT:\s*[a-z_]+\s*$", "", body).strip()
    return body


def _task_id(proposal: str, n: int) -> str:
    first = proposal.strip().splitlines()[0] if proposal.strip() else f"feature-{n}"
    first = re.sub(r"^#+\s*", "", first)
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", first).strip("-").lower()[:50]
    return f"auto-{n}-{slug}" or f"auto-{n}"