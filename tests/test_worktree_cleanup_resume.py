"""Slice 1 tests: worktree cleanup on resume (spec FR-5.3, NFR-3).

Two layers:
  * `external/git_cli.discard_task_residue` — the discard helper itself, in a
    temp git repo (dirty tracked + untracked residue discarded, clean tree a
    no-op, trunk/detached HEAD refused).
  * `Pipeline.process` resume path — residue is gone *before* the next slice
    session is dispatched, the WORKTREE-CLEANUP log line is emitted, a fresh
    intake never triggers cleanup.

Run from the repo root:  python3 -m unittest tests.test_worktree_cleanup_resume
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from external.git_cli import discard_task_residue

from tests.test_pipeline_resume import FakeRunner, _make_repo


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


class DiscardTaskResidueTest(unittest.TestCase):
    """Unit tests for the helper in a bare temp git repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-b", "pi/trunk")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init")
        _git(self.repo, "checkout", "-b", "pi/t1")

    def _make_dirty(self) -> None:
        (self.repo / "tracked.md").write_text("committed\n")
        _git(self.repo, "add", "tracked.md")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "add tracked.md")
        # residue from the killed attempt: modified tracked file, staged
        # change, and an untracked file in an untracked directory
        (self.repo / "tracked.md").write_text("half-written edit\n")
        (self.repo / "staged.md").write_text("staged residue\n")
        _git(self.repo, "add", "staged.md")
        (self.repo / "scratch" / "deep").mkdir(parents=True)
        (self.repo / "scratch" / "deep" / "tmp.txt").write_text("junk\n")

    def test_discards_tracked_staged_and_untracked_residue(self):
        self._make_dirty()
        discarded = discard_task_residue(self.repo, "t1", "pi/trunk")
        # porcelain reports the untracked directory collapsed, not its contents
        self.assertEqual(sorted(discarded),
                         ["scratch/", "staged.md", "tracked.md"])
        # worktree is clean afterwards
        self.assertEqual(_git(self.repo, "status", "--porcelain").strip(), "")
        # committed work survived the discard
        self.assertEqual((self.repo / "tracked.md").read_text(), "committed\n")

    def test_clean_tree_is_a_noop(self):
        self.assertEqual(discard_task_residue(self.repo, "t1", "pi/trunk"), [])
        self.assertEqual(_git(self.repo, "status", "--porcelain").strip(), "")

    def test_refuses_the_trunk_branch(self):
        _git(self.repo, "checkout", "pi/trunk")
        (self.repo / "human.md").write_text("uncommitted human work\n")
        with self.assertRaises(RuntimeError):
            discard_task_residue(self.repo, "t1", "pi/trunk")
        # the refusal is total: nothing was discarded
        self.assertTrue((self.repo / "human.md").exists())

    def test_refuses_a_detached_head(self):
        head = _git(self.repo, "rev-parse", "HEAD").strip()
        _git(self.repo, "checkout", "--detach", head)
        (self.repo / "human.md").write_text("residue\n")
        with self.assertRaises(RuntimeError):
            discard_task_residue(self.repo, "t1", "pi/trunk")
        self.assertTrue((self.repo / "human.md").exists())

    def test_refuses_a_foreign_branch(self):
        _git(self.repo, "checkout", "-b", "pi/t2")
        (self.repo / "human.md").write_text("another task's work\n")
        with self.assertRaises(RuntimeError):
            discard_task_residue(self.repo, "t1", "pi/trunk")
        self.assertTrue((self.repo / "human.md").exists())


class PipelineResumeCleanupTest(unittest.TestCase):
    """The resume path discards residue before the next slice is dispatched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.repo = _make_repo(Path(self._tmp.name) / "repo")
        self.runner = FakeRunner()
        self.pipeline = self._make_pipeline()
        self._seed_slices()

    def _make_pipeline(self):
        from harness.workflow.pipeline import Pipeline
        from tests.test_pipeline_resume import _cfg
        return Pipeline(_cfg(self.queue_dir), self.runner, log=self.lines.append)

    def _seed_slices(self):
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self):
        from harness.core.providers import Task
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _crash_like_resume_state(self):
        """Intake + checkpoint the three pre-slice stages, then leave dirty
        tracked and untracked residue on the task branch (the killed attempt's
        footprint)."""
        from harness.core.enums import CheckpointStage
        self.pipeline.lifecycle.intake(self._task())
        for stage in (CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                      CheckpointStage.SLICING):
            self.pipeline.lifecycle.checkpoint("t1", stage)
        # put the repo on the task branch the way a killed attempt would have
        # left it (ensure_branch re-checks it out idempotently on resume)
        _git(self.repo, "checkout", "-b", "pi/t1")
        (self.repo / "residue.md").write_text("half-written edit\n")
        (self.repo / "untracked-junk.txt").write_text("junk\n")

    def test_resume_discards_residue_before_the_next_slice(self):
        self._crash_like_resume_state()
        # A runner that snapshots `git status` at every session dispatch: the
        # proof is that the first post-resume session sees a clean worktree.
        seen: list[str] = []

        class ObservingRunner(FakeRunner):
            def run(self, model, workdir, prompt, **kw):
                seen.append(_git(Path(workdir), "status", "--porcelain"))
                return super().run(model, workdir, prompt, **kw)

        self.runner = ObservingRunner()
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertTrue(seen, "no session was dispatched")
        for snapshot in seen:
            self.assertEqual(snapshot.strip(), "",
                             "residue leaked into a dispatched session")
        self.assertFalse((self.repo / "residue.md").exists())
        self.assertFalse((self.repo / "untracked-junk.txt").exists())
        joined = "\n".join(self.lines)
        self.assertIn("WORKTREE-CLEANUP t1: discarded", joined)
        self.assertIn("residue.md", joined)

    def test_clean_resume_logs_a_noop_and_completes(self):
        from harness.core.enums import CheckpointStage
        self.pipeline.lifecycle.intake(self._task())
        for stage in (CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                      CheckpointStage.SLICING):
            self.pipeline.lifecycle.checkpoint("t1", stage)
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertIn("WORKTREE-CLEANUP t1: clean (no residue)",
                      "\n".join(self.lines))

    def test_fresh_intake_does_not_run_cleanup(self):
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertNotIn("WORKTREE-CLEANUP", "\n".join(self.lines))

    def test_crash_before_any_checkpoint_still_cleans(self):
        # task.json exists but nothing is checkpointed: the full task re-runs
        # from stage 0 and the cleanup still runs (residue is discarded).
        self.pipeline.lifecycle.intake(self._task())
        (self.repo / "residue.md").write_text("half-written edit\n")
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertFalse((self.repo / "residue.md").exists())
        self.assertIn("WORKTREE-CLEANUP t1: discarded", "\n".join(self.lines))


if __name__ == "__main__":
    unittest.main()
