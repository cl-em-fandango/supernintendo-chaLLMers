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
from ..core.enums import Stage, Verdict
from ..core.providers import Task
from ..core.session import SessionRunner
from ..core.sync_stage_change_hook import run_stage_change_hook


class AutonomousGenerator:
    def __init__(self, cfg: Config, runner: SessionRunner, provider, log=print,
                 sync_engine=None):
        self.cfg = cfg
        self.runner = runner
        self.provider = provider
        self.log = log
        # GitHub sync dispatcher (spec FR-3): a task landing in `pending/`
        # is a stage change. None (GitHub unconfigured) keeps the hook a
        # no-op (FR-0.1, NFR-2); the hook swallows its own failures (NFR-1).
        self.sync_engine = sync_engine

    def _random_model(self, exclude: str | None = None) -> str:
        # Autonomous suggest/review is high-volume and low-stakes -> use fast models.
        pool = [m for m in self.cfg.fast_pool if m != exclude]
        if not pool:
            pool = [m for m in self.cfg.random_pool if m != exclude]
        return random.choice(pool) if pool else self.cfg.random_pool[0]

    def run(self, workdir: Path, stand_down_check=None) -> int:
        """Generate tasks until `pending/` holds the target, and return the count.

        `stand_down_check`, when given, is called at every boundary
        immediately before a `pi` session spawns — the attempt boundary and
        the point between the suggest and the review session of one attempt
        (an attempt is two sessions, so the FR-6.1 boundary is inside it
        too). A True answer means
        an interrupt is active and already acknowledged: the loop stops taking
        work, leaves the queue as it is, and returns what it added so far
        (spec FR-6.1/FR-6.2 — the running session finished normally, nothing
        is parked, the exit is clean).
        """
        target = self.cfg.autonomous_queue_target
        added = 0
        attempts = 0
        max_attempts = target * 6  # safety valve

        while self._pending_count() < target and attempts < max_attempts:
            if stand_down_check is not None and stand_down_check():
                self.log("autonomous mode: stood down at session boundary")
                return added
            attempts += 1
            self.log(f"── autonomous attempt {attempts} "
                     f"(pending={self._pending_count()}/{target}) ──")

            ma = self._random_model()
            r = self.runner.run(
                ma, workdir, prompts.autonomous_suggest(),
                stage=Stage.AUTONOMOUS_SUGGEST, notes="proposal")
            if r.verdict is not Verdict.DONE:
                self.log(f"  suggestion failed (verdict={r.verdict}); retrying")
                continue

            proposal = _proposal_text(r.output)
            if not proposal.strip():
                self.log("  empty proposal; retrying")
                continue

            logs_dir = getattr(self.cfg, "logs_dir", None) or (
                Path(self.cfg.harness_execution_and_queue_dir) / "logs")
            prop_file = logs_dir / f"autonomous-proposal-{attempts}.md"
            prop_file.parent.mkdir(parents=True, exist_ok=True)
            prop_file.write_text(proposal)

            if stand_down_check is not None and stand_down_check():
                # The proposal session finished; the review session would be
                # a fresh spawn, so this is a boundary (FR-6.1). The proposal
                # is already on disk under logs/; nothing is queued, parked
                # or retried (FR-6.2/FR-6.4).
                self.log("autonomous mode: stood down at session boundary")
                return added

            mb = self._random_model(exclude=ma)
            r = self.runner.run(
                mb, workdir, prompts.autonomous_review(prop_file),
                stage=Stage.AUTONOMOUS_REVIEW, notes=f"review of {ma}'s proposal")
            self.log(f"  review ({mb}) verdict: {r.verdict}")
            if r.verdict is not Verdict.PASS:
                self.log("  proposal rejected; trying again")
                continue

            task_id = _task_id(proposal, attempts)
            dest = self.cfg.queue_dir / "pending" / f"{task_id}.md"
            dest.write_text(proposal)
            self.log(f"  ➕ queued task: {task_id}")
            # The task only lands in `pending/` (spec FR-3): one sync pass
            # for it, after the write; failures die inside the hook (NFR-1).
            run_stage_change_hook(self.sync_engine, task_id, log=self.log)
            added += 1

        self.log(f"autonomous mode finished: added {added} tasks "
                 f"({self._pending_count()} pending)")
        return added

    def _pending_count(self) -> int:
        """Queue depth, read-only.

        Counted through `provider.count_pending()` rather than by fetching: a
        fetch is the claim boundary, and this is asked on every loop condition,
        every attempt header and the closing line, so a count that claimed
        would empty the queue by looking at it.
        """
        return self.provider.count_pending()


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