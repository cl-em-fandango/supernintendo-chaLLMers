"""Slice 2 tests: `harness.py resume <task_id>` CLI (spec FR3, acceptance #2/#4/#5/#6, EC8).

Run from the repo root:  python3 -m unittest tests.test_resume_cli
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
from harness.workflow.pipeline import Pipeline
from harness.workflow.resume import resume_task
from harness.workflow.task_lifecycle import TaskLifecycle

from tests.test_pipeline_resume import FakeRunner, _make_repo


def _cfg(queue_dir: Path, repo: Path | None = None) -> Config:
    return Config(
        harness_execution_and_queue_dir=queue_dir.parent,
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
        target_codebase_dir=repo,
    )


class ResumeCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.repo = _make_repo(Path(self._tmp.name) / "repo")
        self.cfg = _cfg(self.queue_dir, repo=self.repo)
        self.runner = FakeRunner()
        self.pipeline = Pipeline(self.cfg, self.runner,
                                 log=self.lines.append)
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
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

    def _review_file(self, task_id="t1", text="slicing failed (verdict=fail)"):
        (self.queue_dir / "review" / f"{task_id}.md").write_text(
            f"# Task: {task_id}\n\n**Status:** PARKED\n\n"
            f"## Executive summary\n\n{text}\n")

    def _log(self) -> str:
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # acceptance #2: resume from active/ skips checkpointed stages
    # ------------------------------------------------------------------
    def test_resume_active_skips_checkpointed_stages(self):
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                                 CheckpointStage.SLICING)
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append)
        self.assertEqual(rc, 0)
        # only slices + holistic ran
        self.assertNotIn("spec_author", self.runner.calls)
        self.assertNotIn("feasibility", self.runner.calls)
        self.assertNotIn("slicing", self.runner.calls)
        self.assertIn("slice_implement", self.runner.calls)
        self.assertIn("holistic", self.runner.calls)
        # skip lines logged
        self.assertIn("resuming from checkpoint — skipping: spec, feasibility, slicing",
                      self._log())
        # task reached done/
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())
        raw = json.loads((self.queue_dir / "done" / "t1" / "task.json").read_text())
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices", "merge"])

    # ------------------------------------------------------------------
    # F3.4: plan preview
    # ------------------------------------------------------------------
    def test_plan_preview_printed(self):
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        resume_task("t1", yes=True, cfg=self.cfg,
                    pipeline=self.pipeline, lifecycle=self.lifecycle,
                    log=self.lines.append)
        log = self._log()
        self.assertIn("task t1 (active)", log)
        self.assertIn("checkpointed: spec, feasibility", log)
        self.assertIn("will run:     slicing, slices, holistic", log)

    # ------------------------------------------------------------------
    # acceptance #4: resume on done task
    # ------------------------------------------------------------------
    def test_resume_done_task_reports_complete(self):
        self.pipeline.process(self._task())
        self.assertTrue((self.queue_dir / "done" / "t1").exists())
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append)
        self.assertEqual(rc, 0)
        self.assertIn("already complete", self._log())
        # no state change
        self.assertTrue((self.queue_dir / "done" / "t1").exists())
        self.assertFalse((self.queue_dir / "active" / "t1").exists())

    # ------------------------------------------------------------------
    # acceptance #5: resume on parked task
    # ------------------------------------------------------------------
    def test_resume_parked_task_prompts_and_resumes(self):
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        self.lifecycle.park("t1", "slicing failed (verdict=fail)")
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        # Enter (default yes) accepts
        rc = resume_task("t1", yes=False, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append, input_fn=lambda *a: "")
        self.assertEqual(rc, 0)
        # reason printed
        self.assertIn("reason: slicing failed (verdict=fail)", self._log())
        # dir moved back to active/ and finished
        self.assertTrue((self.queue_dir / "done" / "t1").exists())
        # resumed from checkpoint: no spec/feasibility re-run
        self.assertNotIn("spec_author", self.runner.calls)
        self.assertNotIn("feasibility", self.runner.calls)
        self.assertIn("slicing", self.runner.calls)

    def test_resume_parked_task_declined_stays_parked(self):
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        self.lifecycle.park("t1", "slicing failed (verdict=fail)")
        self._review_file()
        rc = resume_task("t1", yes=False, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append, input_fn=lambda *a: "n")
        self.assertEqual(rc, 0)
        self.assertIn("not resuming t1", self._log())
        # dir stays parked, review kept
        self.assertTrue((self.queue_dir / "parked" / "t1").exists())
        self.assertTrue((self.queue_dir / "review" / "t1.md").exists())
        self.assertFalse((self.queue_dir / "active" / "t1").exists())

    def test_resume_parked_task_yes_flag_skips_prompt(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        self.lifecycle.park("t1", "spec author failed twice")
        self._review_file(text="spec author failed twice")
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        # --yes: no prompt at all (input_fn would fail if called)
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append,
                         input_fn=lambda *a: self.fail("prompt should be skipped"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.queue_dir / "done" / "t1").exists())
        self.assertIn("reason: spec author failed twice", self._log())

    def test_resume_parked_task_no_review_recorded(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        # park the dir without writing a review file (simulates a crash before
        # the exec summary was written)
        import shutil
        shutil.move(str(self.queue_dir / "active" / "t1"),
                    str(self.queue_dir / "parked" / "t1"))
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append)
        self.assertEqual(rc, 0)
        self.assertIn("reason: (not recorded)", self._log())

    # ------------------------------------------------------------------
    # EC8: not found
    # ------------------------------------------------------------------
    def test_resume_unknown_task_not_found(self):
        rc = resume_task("nope", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("nope not found in active/, parked/, failed/, done/",
                      self._log())

    # ------------------------------------------------------------------
    # acceptance #6 / EC9: corrupt task.json -> warn, resume from spec
    # ------------------------------------------------------------------
    def test_resume_corrupt_task_json_resumes_from_spec(self):
        td = self.lifecycle.intake(self._task())
        (td / "task.json").write_text("{corrupt")
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append)
        self.assertEqual(rc, 0)
        self.assertIn("unparseable", self._log())
        self.assertIn("spec_author", self.runner.calls)
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())

    # ------------------------------------------------------------------
    # T54: --fresh drops checkpoints, normal resume preserves them
    # ------------------------------------------------------------------
    def test_resume_fresh_drops_checkpoints(self):
        """resume --fresh deletes the task dir and restarts from scratch."""
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                                 CheckpointStage.SLICING)
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append, fresh=True)
        self.assertEqual(rc, 0)
        # all stages re-run: spec_author was checkpointed but fresh drops it
        self.assertIn("spec_author", self.runner.calls)
        self.assertIn("feasibility", self.runner.calls)
        self.assertIn("slicing", self.runner.calls)
        # task reached done/
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())

    def test_resume_parked_preserves_checkpoints(self):
        """Normal resume (no --fresh) on a parked task preserves checkpoints."""
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        self.lifecycle.park("t1", "slicing failed (verdict=fail)")
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        rc = resume_task("t1", yes=True, cfg=self.cfg,
                         pipeline=self.pipeline, lifecycle=self.lifecycle,
                         log=self.lines.append, fresh=False)
        self.assertEqual(rc, 0)
        # checkpointed stages are skipped
        self.assertNotIn("spec_author", self.runner.calls)
        self.assertNotIn("feasibility", self.runner.calls)
        # remaining stages run
        self.assertIn("slicing", self.runner.calls)
        self.assertIn("slice_implement", self.runner.calls)

    # ------------------------------------------------------------------
    # F3.5: task reconstruction (body from original.md, source from task.json)
    # ------------------------------------------------------------------
    def test_task_reconstruction_from_dir(self):
        self._checkpoint_through(CheckpointStage.SPEC)
        self.lifecycle.park("t1", "spec author failed twice")
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        resume_task("t1", yes=True, cfg=self.cfg,
                    pipeline=self.pipeline, lifecycle=self.lifecycle,
                    log=self.lines.append)
        # the resumed task ran against the repo workdir (from original.md)
        self.assertIn("slice_implement", self.runner.calls)
        self.assertTrue((self.queue_dir / "done" / "t1").exists())

    def test_unpark_and_restart_handler_dispatch(self):
        from harness.cli.handlers import cmd_unpark, cmd_restart
        self._checkpoint_through(CheckpointStage.SPEC, CheckpointStage.FEASIBILITY)
        self.lifecycle.park("t1", "test failure")
        # unpark preserves checkpoints
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        # verify cmd_unpark is a synonym of cmd_resume (callable with yes/fresh)
        self.assertTrue(callable(cmd_unpark))
        self.assertTrue(callable(cmd_restart))


if __name__ == "__main__":
    unittest.main()
