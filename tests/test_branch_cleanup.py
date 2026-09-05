"""T71 — the feature branch goes only after the task is complete (finding F8).

Two steps end a task: the squash-merge onto trunk, and the `complete()` move
into `done/`. The branch has to survive the first of them — a run that dies in
between still needs it to resume — so deletion lives in its own function,
`cleanup_branch`, and the pipeline calls it only once the completion move has
returned.

Contracts proven here:

  * `merge_to_trunk` never deletes the branch, on the passing path or on the
    gate-failure rollback path;
  * `cleanup_branch` deletes it afterwards, is idempotent, and needs `-D`
    because a squash-merged branch is never an ancestor of trunk;
  * the pipeline's order is merge -> checkpoint(merge) -> complete -> cleanup,
    on both the fresh and the already-merged resume path, and no cleanup runs
    when the merge or the holistic review parked the task;
  * a cleanup that raises is logged with the branch name and the git error and
    changes nothing else: the task stays in `done/`, still `done`, and is
    neither re-parked nor failed.

The git layer is real: every repo is a throwaway `tempfile` tree carrying the
two recognition stubs `gate_applies` looks for (`harness.py`,
`harness/composition.py`), with `verify_harness` patched so the real gate is
never pointed at a scratch tree. The session runner is a stub — this file owns
the ordering around completion, not session behaviour. Nothing here touches
`/home/donald/work/harness` or `/home/donald/work/queue`.

Out of scope: merge/gate behaviour (T63, T64, T65), the merge checkpoint's own
bookkeeping (T70), and any lifecycle move change.

Run from the repo root:  python3 -m unittest tests.test_branch_cleanup -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external import git_cli as G  # noqa: E402
from harness.core import gitops as GIT  # noqa: E402
from harness.core.config import Config  # noqa: E402
from harness.core.enums import CheckpointStage, Verdict  # noqa: E402
from harness.core.providers import Task  # noqa: E402
from harness.core.session import SessionResult  # noqa: E402
from harness.workflow.params import StageContext  # noqa: E402
from harness.workflow.pipeline import Pipeline  # noqa: E402
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

TRUNK = "pi/trunk"
TASK = "t71"
BRANCH = f"pi/{TASK}"
CLEANUP_MSG = "was not deleted"


@dataclass(frozen=True)
class CleanupCall:
    """One recorded `cleanup_branch(workdir, task_id, trunk)` call."""
    workdir: Path
    task_id: str
    trunk: str


class StubRunner:
    """Stands in for `SessionRunner`: every session comes back healthy.

    Session output is written outside the repo on purpose — an untracked `.out`
    file in the worktree would trip `merge_to_trunk`'s uncommitted-work guard
    and the merge under test would never happen.
    """

    def __init__(self, out_dir: Path, verdict: Verdict = Verdict.PASS):
        self.out_dir = out_dir
        self.verdict = verdict
        self.calls: list = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            **kw) -> SessionResult:
        self.calls.append(stage)
        text = f"## Summary\nfeature complete\n\nVERDICT: {self.verdict.value}"
        out_file = self.out_dir / f"session-{stage}-{len(self.calls)}.out"
        out_file.write_text(text)
        return SessionResult(ok=True, verdict=self.verdict, peak_tokens=0,
                             duration_s=0.0, output=text, out_file=out_file,
                             crashed=False)


class RecordingLifecycle(TaskLifecycle):
    """`TaskLifecycle` that records the transitions it is asked to perform."""

    def __init__(self, cfg: Config, log=print, events: list | None = None):
        super().__init__(cfg, log)
        self.events: list[str] = [] if events is None else events

    def checkpoint(self, task_id: str, stage: CheckpointStage,
                   where: str = "active") -> None:
        self.events.append(f"checkpoint:{stage.value}")
        super().checkpoint(task_id, stage, where)

    def complete(self, task_id: str, summary: str) -> None:
        self.events.append("complete")
        super().complete(task_id, summary)

    def park(self, task_id: str, reason: str) -> None:
        self.events.append("park")
        super().park(task_id, reason)

    def fail(self, task_id: str, reason: str) -> None:
        self.events.append("fail")
        super().fail(task_id, reason)


class PostCompleteBranchCleanupTest(unittest.TestCase):
    """One fixture: a queue, a temp repo with a squash-mergeable feature branch,
    and a pipeline whose sessions always pass."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="t71-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()

        self.cfg = Config(
            harness_execution_and_queue_dir=self.root,
            token_budget=100_000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=5,
            trunk_branch=TRUNK,
            task_provider="directory",
            directory_provider={},
            models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
            model_context_map={},
        )
        for sub in ("pending", "claimed", "active", "review", "parked", "failed",
                    "done"):
            (self.cfg.queue_dir / sub).mkdir(parents=True)

        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._init_repo()

        # The verification gate can only judge the real harness, so its answer
        # is supplied for the whole test; individual tests override it.
        mock.patch.object(G, "verify_harness",
                          return_value=(True, "ok")).start()
        self.addCleanup(mock.patch.stopall)

        self.lines: list[str] = []
        self.events: list[str] = []
        self.lifecycle = RecordingLifecycle(self.cfg, log=self.lines.append,
                                            events=self.events)
        self.runner = StubRunner(self.sessions_dir)
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)
        self.pipeline.lifecycle = self.lifecycle
        self.task_dir = self.lifecycle.intake(
            Task(id=TASK, body="# t71\n\nmerge then clean up the branch\n",
                 source=f"directory:{TASK}.md"))
        self.cleanup_calls: list[CleanupCall] = []

    # ------------------------------------------------------------------
    # repo helpers
    # ------------------------------------------------------------------
    def _write(self, rel: str, text: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.repo,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, (args, proc.stderr))
        return proc.stdout

    def _commit(self, msg: str) -> None:
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)

    def _init_repo(self) -> None:
        """Trunk with one commit plus `pi/<TASK>` carrying one edit and one
        added file, ending on trunk with a clean tree."""
        self._git("init", "-b", TRUNK)
        self._write("harness.py", "# gate recognition stub\n")
        self._write("harness/composition.py", "# gate recognition stub\n")
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("base")
        self.base_sha = self.rev(TRUNK)
        self._git("checkout", "-b", BRANCH)
        self._write("f.txt", "base + work\n")
        self._write("feature.txt", "work product\n")
        self._git("add", "-A")
        self._commit("feat:t71")
        self._git("checkout", TRUNK)
        self.branch_sha = self.rev(BRANCH)

    def rev(self, ref: str) -> str:
        """The commit `ref` resolves to (tags peeled, branches followed)."""
        return self._git("rev-parse", f"{ref}^{{commit}}").strip()

    def porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    # ------------------------------------------------------------------
    # git-layer helpers
    # ------------------------------------------------------------------
    def _merge(self) -> None:
        """The real `merge_to_trunk`; `setUp` already fixed the gate's answer."""
        G.merge_to_trunk(self.repo, TASK, TRUNK, "title")

    def _failed_merge(self, detail: str = "import failed: boom") -> str:
        """Run the merge past a failing gate and return the exception message."""
        with mock.patch.object(G, "verify_harness", return_value=(False, detail)):
            with self.assertRaises(RuntimeError) as ctx:
                G.merge_to_trunk(self.repo, TASK, TRUNK, "title")
        return str(ctx.exception)

    # ------------------------------------------------------------------
    # pipeline-layer helpers
    # ------------------------------------------------------------------
    def _ctx(self) -> StageContext:
        return StageContext(TASK, self.task_dir, self.repo)

    def _stub_merge(self, error: Exception | None = None):
        def merge(workdir, task_id, trunk, title) -> None:
            self.events.append("merge")
            if error is not None:
                raise error
        return merge

    def _real_merge(self):
        """The real squash merge, recorded, gate answer supplied by `setUp`."""
        def merge(workdir, task_id, trunk, title) -> None:
            self.events.append("merge")
            G.merge_to_trunk(Path(workdir), task_id, trunk, title)
        return merge

    def _stub_cleanup(self, error: Exception | None = None):
        def cleanup(workdir, task_id, trunk) -> None:
            self.cleanup_calls.append(CleanupCall(Path(workdir), task_id, trunk))
            self.events.append("cleanup")
            if error is not None:
                raise error
        return cleanup

    def _real_cleanup(self):
        """The real `cleanup_branch`, recorded, so the end-to-end tests can see
        the call *and* watch the ref actually go."""
        def cleanup(workdir, task_id, trunk) -> None:
            self.cleanup_calls.append(CleanupCall(Path(workdir), task_id, trunk))
            self.events.append("cleanup")
            G.cleanup_branch(Path(workdir), task_id, trunk)
        return cleanup

    def _install_gitops(self, merge=None, cleanup=None) -> None:
        """Patch the two `harness.core.gitops` names the pipeline imports.

        Both call sites import at call time, so replacing the module attribute
        is what the pipeline actually sees; `stopall` puts it back.
        """
        mock.patch.object(GIT, "merge_to_trunk",
                          merge or self._stub_merge()).start()
        mock.patch.object(GIT, "cleanup_branch",
                          cleanup or self._stub_cleanup()).start()

    # ------------------------------------------------------------------
    # shared assertions
    # ------------------------------------------------------------------
    def _log(self) -> str:
        return "\n".join(self.lines)

    def _status_on_disk(self, where: str) -> str:
        path = self.cfg.queue_dir / where / TASK / "task.json"
        return json.loads(path.read_text())["status"]

    def _assert_still_done(self) -> None:
        """The completed task is untouched by a failed cleanup: same directory,
        same status, same review summary, and no second terminal move."""
        self.assertTrue((self.cfg.queue_dir / "done" / TASK).is_dir(),
                        "a failed cleanup moved the task out of done/")
        self.assertFalse((self.cfg.queue_dir / "parked" / TASK).exists(),
                         "a failed cleanup re-parked a completed task")
        self.assertFalse((self.cfg.queue_dir / "failed" / TASK).exists(),
                         "a failed cleanup failed a completed task")
        self.assertEqual(self._status_on_disk("done"), "done")
        summary = (self.cfg.queue_dir / "review" / f"{TASK}.md").read_text()
        self.assertIn("**Status:** DONE", summary)
        self.assertNotIn("PARKED", self._log())
        self.assertNotIn("KICKED OUT", self._log())
        self.assertNotIn("park", self.events)
        self.assertNotIn("fail", self.events)

    def _assert_cleanup_logged(self, error: Exception) -> None:
        log = self._log()
        self.assertIn(CLEANUP_MSG, log, "the failed cleanup was not logged")
        self.assertIn(BRANCH, log, "the log does not name the branch left behind")
        self.assertIn(str(error), log, "the git error is missing from the log")

    # ------------------------------------------------------------------
    # the merge no longer deletes anything
    # ------------------------------------------------------------------
    def test_merge_to_trunk_leaves_the_feature_branch_in_place(self):
        self._merge()

        self.assertTrue(G.has_branch(self.repo, BRANCH),
                        "merge_to_trunk deleted the feature branch")
        self.assertEqual(self.rev(BRANCH), self.branch_sha,
                         "the merge moved the feature branch")
        self.assertEqual(self.rev(TRUNK), self.rev(G.LAST_GOOD_TAG))
        self.assertEqual(self.current_branch(), TRUNK)

    def test_gate_failure_keeps_the_feature_branch_for_a_resume(self):
        msg = self._failed_merge()

        self.assertIn("verification gate FAILED", msg)
        self.assertTrue(G.has_branch(self.repo, BRANCH),
                        "the rollback deleted the feature branch")
        self.assertEqual(self.rev(BRANCH), self.branch_sha)
        self.assertEqual(self.rev(TRUNK), self.base_sha,
                         "trunk was not rolled back")

    def test_cleanup_branch_is_reachable_through_the_gitops_wrapper(self):
        """The pipeline imports `cleanup_branch` from `harness.core.gitops`;
        that name must be the git_cli operation, not a stale re-export."""
        self.assertIs(GIT.cleanup_branch, G.cleanup_branch)

    # ------------------------------------------------------------------
    # cleanup_branch deletes it afterwards
    # ------------------------------------------------------------------
    def test_cleanup_branch_deletes_the_merged_branch(self):
        self._merge()
        merged = self.rev(TRUNK)

        G.cleanup_branch(self.repo, TASK, TRUNK)

        self.assertFalse(G.has_branch(self.repo, BRANCH),
                         "cleanup_branch did not delete the feature branch")
        self.assertEqual(self.rev(TRUNK), merged, "cleanup moved trunk")
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), merged,
                         "cleanup disturbed the last-good tag")
        self.assertEqual((self.repo / "feature.txt").read_text(), "work product\n",
                         "the merged work went away with the branch")
        self.assertEqual(self.current_branch(), TRUNK)
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")

    def test_cleanup_branch_deletes_a_squash_merged_branch_git_calls_unmerged(self):
        """`-D`, not `-d`: a squash lands the content without making the branch
        an ancestor of trunk, so `git branch -d` refuses forever — the `-d` this
        replaces silently deleted nothing."""
        self._merge()
        refused = subprocess.run(["git", "branch", "-d", BRANCH], cwd=self.repo,
                                 capture_output=True, text=True)
        self.assertNotEqual(refused.returncode, 0,
                            "git branch -d accepted a squash-merged branch; "
                            "this fixture no longer proves the -D requirement")

        G.cleanup_branch(self.repo, TASK, TRUNK)

        self.assertFalse(G.has_branch(self.repo, BRANCH))

    def test_cleanup_branch_is_idempotent_for_a_resumed_task(self):
        self._merge()
        G.cleanup_branch(self.repo, TASK, TRUNK)

        G.cleanup_branch(self.repo, TASK, TRUNK)

        self.assertFalse(G.has_branch(self.repo, BRANCH))
        self.assertEqual(self.current_branch(), TRUNK)
        self.assertEqual(self.porcelain(), "")

    def test_cleanup_branch_from_the_branch_itself_returns_to_trunk(self):
        self._merge()
        self._git("checkout", BRANCH)

        G.cleanup_branch(self.repo, TASK, TRUNK)

        self.assertEqual(self.current_branch(), TRUNK)
        self.assertFalse(G.has_branch(self.repo, BRANCH))
        self.assertEqual(self.porcelain(), "")

    # ------------------------------------------------------------------
    # the pipeline calls it only after complete succeeded
    # ------------------------------------------------------------------
    def test_cleanup_runs_after_the_completion_move(self):
        self._install_gitops()

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(self.events,
                         ["merge", "checkpoint:merge", "complete", "cleanup"],
                         "cleanup did not run exactly once, after complete")
        # the completion move really had landed before cleanup was called
        self.assertEqual(self._status_on_disk("done"), "done")
        self.assertEqual(self.cleanup_calls,
                         [CleanupCall(self.repo, TASK, TRUNK)])

    def test_cleanup_runs_after_complete_on_the_resume_path(self):
        """Already merged: no second merge, no holistic session, but the branch
        still goes once the lost completion move has been redone."""
        self.lifecycle.checkpoint(TASK, CheckpointStage.MERGE)
        self.events.clear()
        self._install_gitops()

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(self.events, ["complete", "cleanup"])
        self.assertEqual(self.runner.calls, [],
                         "a holistic session ran for already-merged work")
        self.assertEqual(self._status_on_disk("done"), "done")

    def test_no_cleanup_when_the_merge_fails(self):
        self._install_gitops(merge=self._stub_merge(
            error=RuntimeError("merge conflict for t71")))

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "parked")
        self.assertEqual(self.events, ["merge", "park"])
        self.assertEqual(self.cleanup_calls, [],
                         "a parked task had its branch deleted")
        self.assertTrue(G.has_branch(self.repo, BRANCH),
                        "the parked task's branch is gone")

    def test_no_cleanup_when_the_holistic_review_fails(self):
        self.runner.verdict = Verdict.FAIL
        self._install_gitops()

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "parked")
        self.assertEqual(self.events, ["park"])
        self.assertEqual(self.cleanup_calls, [])
        self.assertTrue(G.has_branch(self.repo, BRANCH),
                        "the parked task's branch is gone")

    def test_real_merge_complete_cleanup_sequence(self):
        """The whole happy path over a real repo: the work is on trunk, the tag
        names it, the task is in done/, and no branch remains."""
        self._install_gitops(merge=self._real_merge(),
                             cleanup=self._real_cleanup())

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(self.events,
                         ["merge", "checkpoint:merge", "complete", "cleanup"])
        self.assertFalse(G.has_branch(self.repo, BRANCH),
                         "the branch survived the completed task")
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.rev(TRUNK))
        self.assertEqual((self.repo / "feature.txt").read_text(), "work product\n")
        self.assertEqual(self._status_on_disk("done"), "done")
        self.assertEqual(self.porcelain(), "")

    # ------------------------------------------------------------------
    # a failed cleanup is cosmetic
    # ------------------------------------------------------------------
    def test_cleanup_failure_leaves_the_completed_task_done(self):
        boom = RuntimeError("git branch -D pi/t71 failed: error: cannot delete branch")
        self._install_gitops(merge=self._real_merge(),
                             cleanup=self._stub_cleanup(error=boom))

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done",
                         "a failed cleanup changed the pipeline's verdict")
        self.assertEqual(self.events,
                         ["merge", "checkpoint:merge", "complete", "cleanup"])
        self._assert_cleanup_logged(boom)
        self._assert_still_done()
        self.assertTrue(G.has_branch(self.repo, BRANCH),
                        "the branch disappeared despite the failed cleanup")
        self.assertEqual(self.rev(TRUNK), self.rev(G.LAST_GOOD_TAG),
                         "the merged trunk was disturbed by the failed cleanup")

    def test_cleanup_failure_on_the_resume_path_leaves_the_task_done(self):
        """The resume path completes again, so it has to swallow a cleanup
        failure the same way — a re-park here would undo a finished task."""
        self.lifecycle.checkpoint(TASK, CheckpointStage.MERGE)
        self.events.clear()
        boom = RuntimeError("refusing cleanup_branch checkout pi/trunk: dirty")
        self._install_gitops(merge=self._real_merge(),
                             cleanup=self._stub_cleanup(error=boom))

        status = self.pipeline.stage_holistic(self._ctx())

        self.assertEqual(status, "done")
        self.assertEqual(self.events, ["complete", "cleanup"])
        self._assert_cleanup_logged(boom)
        self._assert_still_done()

    def test_cleanup_failure_does_not_escape_the_pipeline(self):
        """`_cleanup_branch` is the boundary: whatever git raises, the call
        returns normally so the caller's `return "done"` is still reached."""
        boom = OSError("task dir vanished mid-cleanup")
        self._install_gitops(cleanup=self._stub_cleanup(error=boom))

        try:
            self.pipeline._cleanup_branch(self.repo, TASK)
        except Exception as exc:
            self.fail(f"cleanup failure escaped the pipeline: {exc!r}")

        self._assert_cleanup_logged(boom)


if __name__ == "__main__":
    unittest.main()
