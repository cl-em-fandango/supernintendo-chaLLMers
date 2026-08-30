"""T23 tests: nothing may `git init` inside the queue tree (F7).

Two layers are proven here:

* `external.git_cli.is_under_queue` — the pure path predicate, including `str`
  arguments and the "prefix string is not containment" case;
* `Pipeline.process()` — a task whose resolved workdir falls under
  `cfg.queue_dir` (or at a path that is not a directory at all) is parked
  before `ensure_branch` is reached, with a reason naming both paths, and no
  `.git` appears anywhere under the temp root.

Run from the repo root:  python3 -m unittest tests.test_queue_git_guard
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from external.git_cli import is_under_queue
from harness.core.config import Config
from harness.core.providers import Task
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle


def _cfg(queue_dir: Path) -> Config:
    """A Config whose `queue_dir` is `queue_dir` (queue_dir.parent == work_dir)."""
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


class StubRunner:
    """Stands in for SessionRunner without a subprocess. Records every call; a
    guarded task must never produce one."""

    def __init__(self):
        self.calls: list[str] = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            slice_id=None, iteration=1, notes=""):
        self.calls.append(str(stage))
        raise AssertionError(f"a session ran for a task that should never reach one: {stage}")


class IsUnderQueueTest(unittest.TestCase):
    def setUp(self):
        self.queue = Path(tempfile.mkdtemp()) / "queue"
        self.queue.mkdir(parents=True)

    def test_task_dir_under_queue(self):
        self.assertTrue(is_under_queue(self.queue / "active" / "002", self.queue))

    def test_queue_itself(self):
        self.assertTrue(is_under_queue(self.queue, self.queue))

    def test_unrelated_path(self):
        self.assertFalse(is_under_queue(Path("/tmp/real/repo"), self.queue))

    def test_prefix_string_trick_is_not_containment(self):
        sibling = self.queue.parent / "queue-totally-unrelated"
        sibling.mkdir()
        self.assertFalse(is_under_queue(sibling, self.queue))

    def test_string_arguments_are_accepted(self):
        # A TypeError from inside a guard is how a guard gets removed, so str
        # must work exactly like Path.
        self.assertTrue(is_under_queue(str(self.queue / "active" / "t1"), str(self.queue)))
        self.assertFalse(is_under_queue("/tmp/queue-totally-unrelated", str(self.queue)))


class QueueWorkdirGuardTest(unittest.TestCase):
    """`process()` on a task that names no repo: parked, never `git init`ed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.queue_dir = self.root / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.cfg = _cfg(self.queue_dir)
        self.lines: list[str] = []
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        # Stub runner: no subprocess, and it records whether a session ever ran.
        self.runner = StubRunner()
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)
        self.pipeline.lifecycle = self.lifecycle
        # Any real git work would be a bug here: make it loud and countable.
        self.ensure_branch_calls: list[str] = []
        import harness.workflow.pipeline as pm
        self._real = pm.ensure_branch

        def refuse(workdir, task_id, trunk):
            # Reaching this at all is the bug under test; recording the workdir
            # lets the assertions say *what* would have been initialised.
            self.ensure_branch_calls.append(str(workdir))
            raise AssertionError(f"ensure_branch must not run: {workdir}")

        pm.ensure_branch = refuse
        self.addCleanup(setattr, pm, "ensure_branch", self._real)

    def _task(self, body: str) -> Task:
        return Task(id="t1", body=body, source="directory:t1.md")

    def _review(self) -> str:
        return (self.queue_dir / "review" / "t1.md").read_text()

    def _assert_no_git_anywhere(self) -> None:
        found = [str(p) for p in self.root.rglob(".git")]
        self.assertEqual([], found, f"a .git appeared under {self.root}: {found}")

    def test_queue_resolved_task_parks_without_git_init(self):
        status = self.pipeline.process(self._task("no repo named here at all\n"))

        self.assertEqual("parked", status)
        # (a) the task ended in parked/
        self.assertTrue((self.queue_dir / "parked" / "t1").is_dir())
        self.assertFalse((self.queue_dir / "active" / "t1").exists())
        # ensure_branch was never reached, so no session ran either
        self.assertEqual([], self.ensure_branch_calls)
        self.assertEqual([], self.runner.calls)
        # the guard logged the workdir it refused
        log = "\n".join(self.lines)
        self.assertIn("refusing to init a repo in the queue", log)
        self.assertIn(str(self.queue_dir), log)
        # (b) the park reason names both paths, and reaches the operator via
        # the review summary `resume <id>` reads
        reason = self._review()
        self.assertIn("PARKED", reason)
        self.assertIn(str(self.queue_dir / "active" / "t1"), reason)
        self.assertIn(str(self.queue_dir), reason)
        self.assertIn("refusing to init a repo in the queue", reason)
        # (c) no git repo was created anywhere under the temp root
        self._assert_no_git_anywhere()

    def test_missing_workdir_parks_without_mkdir(self):
        ghost = self.root / "not-a-repo"
        self.cfg.repo_dir = ghost
        status = self.pipeline.process(self._task(f"work in {ghost}\n"))

        self.assertEqual("parked", status)
        self.assertTrue((self.queue_dir / "parked" / "t1").is_dir())
        self.assertEqual([], self.ensure_branch_calls)
        self.assertFalse(ghost.exists())          # the guard never mkdirs
        reason = self._review()
        self.assertIn(str(ghost), reason)
        self.assertIn("does not exist", reason)
        self._assert_no_git_anywhere()


if __name__ == "__main__":
    unittest.main()
