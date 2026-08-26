"""Slice 3 tests: `--continue` / `--fresh` flags + supervisor integration
(spec F4, F5, acceptance #3/#8, EC11).

Run from the repo root:  python3 -m unittest tests.test_continue_fresh
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import CheckpointStage
from harness.core.providers import Task
from harness.workflow.continue_fresh import (
    fresh_restart,
    in_flight_task_dirs,
    resume_in_flight,
    task_from_dir,
)
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle

from tests.test_pipeline_resume import FakeRunner, _make_repo


def _cfg(queue_dir: Path) -> Config:
    return Config(
        work_dir=queue_dir.parent,
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


class ContinueFreshTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.repo = _make_repo(Path(self._tmp.name) / "repo")
        self.runner = FakeRunner()
        self.pipeline = Pipeline(_cfg(self.queue_dir), self.runner,
                                 log=self.lines.append)
        self.lifecycle = TaskLifecycle(_cfg(self.queue_dir), log=self.lines.append)
        self._seed_slices()

    def _seed_slices(self):
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self):
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _checkpoint_through(self, *stages):
        self.lifecycle.intake(self._task())
        for stage in stages:
            self.lifecycle.checkpoint("t1", stage)

    def _log(self) -> str:
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # F4.2: in-flight scan matches only dirs with a task.json
    # ------------------------------------------------------------------
    def test_in_flight_scan_matches_only_task_json_dirs(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        # orphan dir: no task.json (crash between mkdir and first write)
        orphan = self.queue_dir / "active" / "orphan"
        (orphan / "artifacts").mkdir(parents=True)
        dirs = in_flight_task_dirs(self.lifecycle)
        self.assertEqual([d.name for d in dirs], ["t1"])

    # ------------------------------------------------------------------
    # F3.5: task reconstruction from an active/ dir
    # ------------------------------------------------------------------
    def test_task_from_dir(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        task = task_from_dir(self.lifecycle.task_dir("t1"), self.lifecycle)
        self.assertEqual(task.id, "t1")
        self.assertEqual(task.source, "directory:t1.md")
        self.assertIn(self.repo.as_posix(), task.body)

    def test_task_from_dir_missing_original_md(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        (self.queue_dir / "active" / "t1" / "original.md").unlink()
        task = task_from_dir(self.lifecycle.task_dir("t1"), self.lifecycle)
        self.assertEqual(task.body, "")
        self.assertEqual(task.source, "directory:t1.md")

    # ------------------------------------------------------------------
    # acceptance #3: --continue resumes a stuck in-flight task
    # ------------------------------------------------------------------
    def test_continue_resumes_in_flight_task(self):
        # Simulate a crash mid-stage_slices: checkpoints exist, dir in active/.
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                                 CheckpointStage.SLICING)
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        resume_in_flight(self.lifecycle, self.pipeline, log=self.lines.append)
        # only slices + holistic ran
        self.assertNotIn("spec_author", self.runner.calls)
        self.assertNotIn("feasibility", self.runner.calls)
        self.assertNotIn("slicing", self.runner.calls)
        self.assertIn("slice_implement", self.runner.calls)
        self.assertIn("holistic", self.runner.calls)
        # task finished
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())
        raw = json.loads((self.queue_dir / "done" / "t1" / "task.json").read_text())
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices"])
        # F4.3 log line
        self.assertIn("resuming 1 in-flight task(s) from active/", self._log())

    def test_continue_with_no_in_flight_is_noop(self):
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        n = resume_in_flight(self.lifecycle, self.pipeline, log=self.lines.append)
        self.assertEqual(n, 0)
        self.assertEqual(self.runner.calls, [])
        self.assertNotIn("in-flight", self._log())

    def test_continue_leaves_orphan_dirs_alone(self):
        orphan = self.queue_dir / "active" / "orphan"
        (orphan / "artifacts").mkdir(parents=True)
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        n = resume_in_flight(self.lifecycle, self.pipeline, log=self.lines.append)
        self.assertEqual(n, 0)
        self.assertTrue((orphan / "artifacts").exists())

    # ------------------------------------------------------------------
    # F4.4 / acceptance #8: --fresh deletes the active/ dir and restarts
    # ------------------------------------------------------------------
    def test_fresh_restart_deletes_active_dir_and_review(self):
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        (self.queue_dir / "review" / "t1.md").write_text("stale review")
        fresh_restart("t1", _cfg(self.queue_dir), log=self.lines.append)
        self.assertFalse((self.queue_dir / "active" / "t1").exists())
        self.assertFalse((self.queue_dir / "review" / "t1.md").exists())
        self.assertIn("--fresh: deleted", self._log())

    def test_fresh_restart_unknown_task_is_noop(self):
        fresh_restart("nope", _cfg(self.queue_dir), log=self.lines.append)
        self.assertNotIn("--fresh: deleted", self._log())

    def test_fresh_run_reruns_full_waterfall(self):
        # A checkpointed task that would otherwise resume: --fresh forces a
        # full restart from spec (EC11).
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                                 CheckpointStage.SLICING)
        fresh_restart("t1", _cfg(self.queue_dir), log=self.lines.append)
        # the fresh dir has no artifacts yet; seed slices so stage_slices has work
        self._seed_slices()
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        # the full waterfall ran from spec
        self.assertIn("spec_author", self.runner.calls)
        self.assertIn("feasibility", self.runner.calls)
        self.assertIn("slicing", self.runner.calls)
        self.assertIn("slice_implement", self.runner.calls)
        raw = json.loads((self.queue_dir / "done" / "t1" / "task.json").read_text())
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices"])


if __name__ == "__main__":
    unittest.main()
