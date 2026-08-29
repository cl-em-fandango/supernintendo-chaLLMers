"""T70: the merge checkpoint and its resume routing (finding F8).

A successful squash-merge and the `complete()` move are two steps. A crash in
between must not re-run `merge --squash` on resume: the marker
`CheckpointStage.MERGE` is written after the merge returns and before
completion, and `stage_holistic` honours it by going straight to `complete()`.

The git layer is stubbed — this file owns the checkpoint bookkeeping around a
merge, not git behaviour (that is `tests/test_git_*.py`).

Run from the repo root:  python3 -m unittest tests.test_merge_checkpoint
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import CHECKPOINT_ORDER, CheckpointStage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline, STAGE_SEQUENCE
from harness.workflow.task_lifecycle import TaskLifecycle


@dataclass(frozen=True)
class MergeCall:
    """One recorded `merge_to_trunk(workdir, task_id, trunk, title)` call."""
    workdir: Path
    task_id: str
    trunk: str
    title: str


class StubMerge:
    """Stands in for `gitops.merge_to_trunk`: records every call and can fail."""

    def __init__(self, error: Exception | None = None):
        self.calls: list[MergeCall] = []
        self.error = error

    def __call__(self, workdir, task_id, trunk, title) -> None:
        self.calls.append(MergeCall(Path(workdir), task_id, trunk, title))
        if self.error is not None:
            raise self.error


class StubRunner:
    """Stands in for `SessionRunner`: records the stages it was asked to run."""

    def __init__(self, verdict: Verdict = Verdict.PASS):
        self.calls: list[str] = []
        self.verdict = verdict

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            **kw) -> SessionResult:
        self.calls.append(stage)
        out_file = Path(workdir) / f".pi-session-{stage}-{len(self.calls)}.out"
        out_file.write_text("VERDICT: " + self.verdict)
        return SessionResult(ok=True, verdict=self.verdict, peak_tokens=0,
                             duration_s=0.0,
                             output="## Summary\nmerged work\n\nVERDICT: " + self.verdict.value,
                             out_file=out_file, crashed=False)


class RecordingLifecycle(TaskLifecycle):
    """`TaskLifecycle` that records the checkpoint/complete calls it receives."""

    def __init__(self, cfg: Config, log=print):
        super().__init__(cfg, log)
        self.events: list[str] = []

    def checkpoint(self, task_id: str, stage: CheckpointStage,
                   where: str = "active") -> None:
        self.events.append(f"checkpoint:{stage.value}")
        super().checkpoint(task_id, stage, where)

    def complete(self, task_id: str, summary: str) -> None:
        self.events.append("complete")
        super().complete(task_id, summary)


class FailingCheckpointLifecycle(RecordingLifecycle):
    """Lifecycle whose checkpoint write blows up (disk error, partial crash)."""

    def checkpoint(self, task_id: str, stage: CheckpointStage,
                   where: str = "active") -> None:
        self.events.append(f"checkpoint:{stage.value}")
        raise OSError("task.json write failed")


def _cfg(work_dir: Path) -> Config:
    return Config(
        work_dir=work_dir,
        token_budget=100_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
        model_context_map={},
    )


class MergeCheckpointTest(unittest.TestCase):
    """`stage_holistic`'s merge/checkpoint/complete routing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.cfg = _cfg(self.work_dir)
        self.queue_dir = self.cfg.queue_dir
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.lifecycle = RecordingLifecycle(self.cfg, log=self.lines.append)
        self.runner = StubRunner()
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)
        self.pipeline.lifecycle = self.lifecycle
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.task_dir = self.lifecycle.intake(
            Task(id="t1", body="# t1\n\nrequirement\n", source="directory:t1.md"))

    def _ctx(self) -> StageContext:
        return StageContext("t1", self.task_dir, self.work_repo)

    def _done_state(self) -> dict:
        import json
        return json.loads((self.queue_dir / "done" / "t1" / "task.json").read_text())

    def _log(self) -> str:
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # contract: MERGE is a checkpointable stage, ordered after SLICES
    # ------------------------------------------------------------------
    def test_merge_stage_is_ordered_after_slices(self):
        self.assertEqual(CheckpointStage.MERGE.value, "merge")
        self.assertEqual(CHECKPOINT_ORDER[-1], CheckpointStage.MERGE)
        self.assertEqual(CHECKPOINT_ORDER[-2], CheckpointStage.SLICES)
        # the marker has no stage function of its own, so it must not appear in
        # the waterfall process() runs (it would be looked up and blow up).
        self.assertNotIn(CheckpointStage.MERGE, STAGE_SEQUENCE)

    # ------------------------------------------------------------------
    # happy path: merge -> checkpoint(merge) -> complete
    # ------------------------------------------------------------------
    def test_merge_is_checkpointed_before_complete(self):
        merge = StubMerge()
        with patch("harness.core.gitops.merge_to_trunk", merge):
            status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual([c.task_id for c in merge.calls], ["t1"])
        self.assertEqual(merge.calls[0].trunk, "pi/trunk")
        self.assertEqual(merge.calls[0].workdir, self.work_repo)
        # the checkpoint lands after the merge and before the completion move
        self.assertEqual(self.lifecycle.events,
                         ["checkpoint:merge", "complete"])
        # and it is on disk in the completed task
        self.assertEqual(self._done_state()["checkpointed_stages"], ["merge"])

    # ------------------------------------------------------------------
    # resume path: already merged -> no second merge, straight to complete
    # ------------------------------------------------------------------
    def test_already_merged_run_skips_merge_and_completes(self):
        self.lifecycle.checkpoint("t1", CheckpointStage.MERGE)
        self.lifecycle.events.clear()
        merge = StubMerge()
        with patch("harness.core.gitops.merge_to_trunk", merge):
            status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(merge.calls, [], "merge re-ran for already-merged work")
        self.assertEqual(self.runner.calls, [], "a session ran for already-merged work")
        self.assertIn("already merged, completing", self._log())
        # completion still happened, exactly once, and the marker is preserved
        self.assertEqual(self.lifecycle.events, ["complete"])
        self.assertEqual(self._done_state()["checkpointed_stages"], ["merge"])

    # ------------------------------------------------------------------
    # failure path 1: a lost checkpoint write must not strand a merged task
    # ------------------------------------------------------------------
    def test_lost_checkpoint_write_still_completes(self):
        failing = FailingCheckpointLifecycle(self.cfg, log=self.lines.append)
        self.pipeline.lifecycle = failing
        merge = StubMerge()
        with patch("harness.core.gitops.merge_to_trunk", merge):
            status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(len(merge.calls), 1)
        self.assertIn("could not checkpoint the merge", self._log())
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())
        self.assertFalse((self.queue_dir / "active" / "t1").exists())

    # ------------------------------------------------------------------
    # failure path 2: a failed merge records nothing and parks
    # ------------------------------------------------------------------
    def test_failed_merge_records_no_checkpoint(self):
        merge = StubMerge(error=RuntimeError("merge conflict"))
        with patch("harness.core.gitops.merge_to_trunk", merge):
            status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "parked")
        self.assertEqual(self.lifecycle.events, [])
        self.assertTrue((self.queue_dir / "parked" / "t1").exists())
        self.assertFalse((self.queue_dir / "done" / "t1").exists())


if __name__ == "__main__":
    unittest.main()
