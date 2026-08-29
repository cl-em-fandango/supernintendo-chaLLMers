"""T45 tests: a terminal move survives a bookkeeping write failure.

The directory move is the lifecycle authority. Two contracts are proven here
against `TaskLifecycle.park/fail/complete`:

* once `shutil.move` has succeeded, a failure updating `task.json` or writing
  the review summary is logged with the offending path and does *not* escape —
  the task stays in the terminal directory it was moved to, and the two
  bookkeeping steps are independent of each other;
* a failure of the move itself escapes, and no terminal bookkeeping is written
  anywhere (no `task.json` stamp, no review summary).

The state-write failures are monkeypatched; the summary-write and one
move-failure case are produced by the real filesystem (a directory sitting on
the path a file write wants), so neither depends on a stub standing in for the
code under test.

Run from the repo root:  python3 -m unittest tests.test_terminal_move_failures
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.providers import Task
from harness.workflow import task_lifecycle as tl
from harness.workflow.task_lifecycle import TaskLifecycle


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


class TerminalMoveFailureTest(unittest.TestCase):
    """`park`/`fail`/`complete` on a task whose post-move writes fail."""

    TASK_ID = "t45"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.lifecycle = TaskLifecycle(_cfg(self.queue_dir), log=self.lines.append)
        self.lifecycle.intake(Task(id=self.TASK_ID, body="# t45\n\nbody",
                                   source=f"directory:{self.TASK_ID}.md"))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _monkeypatch(self, owner, name, replacement):
        original = getattr(owner, name)
        setattr(owner, name, replacement)
        self.addCleanup(setattr, owner, name, original)

    def _break_state_write(self, message="No space left on device"):
        """Make the `task.json` write raise, as a full or read-only disk would.
        Installed on the class, so it takes the bound `self` first."""
        def explode(self_, state, where="active"):
            raise OSError(message)
        self._monkeypatch(TaskLifecycle, "save_state", explode)
        return message

    def _break_summary_write(self) -> None:
        """Put a directory where the review summary file belongs, so the real
        `write_text` raises `IsADirectoryError`."""
        self.review_path.mkdir(parents=True)

    @property
    def review_path(self) -> Path:
        return self.queue_dir / "review" / f"{self.TASK_ID}.md"

    def _status_on_disk(self, where: str) -> str:
        raw = json.loads((self.queue_dir / where / self.TASK_ID / "task.json").read_text())
        return raw["status"]

    def _log(self) -> str:
        return "\n".join(self.lines)

    def _assert_moved_to(self, where: str) -> None:
        self.assertTrue((self.queue_dir / where / self.TASK_ID).is_dir(),
                        f"{self.TASK_ID} did not land in {where}/")
        self.assertFalse((self.queue_dir / "active" / self.TASK_ID).exists(),
                         f"{self.TASK_ID} is still in active/")

    # ------------------------------------------------------------------
    # task.json write failure after a successful move
    # ------------------------------------------------------------------
    def _check_state_write_failure(self, verb: str, where: str, summary_heading: str):
        message = self._break_state_write()
        getattr(self.lifecycle, verb)(self.TASK_ID, "reason text")   # must not raise

        self._assert_moved_to(where)
        # the failure is observable: path + exception, and no claim of success
        log = self._log()
        self.assertIn(str(self.queue_dir / where / self.TASK_ID / "task.json"), log)
        self.assertIn(message, log)
        self.assertIn("was not updated", log)
        # the review summary is a separate step and still gets written
        self.assertIn(summary_heading, self.review_path.read_text())

    def test_park_survives_task_json_write_failure(self):
        self._check_state_write_failure("park", "parked", "**Status:** PARKED")

    def test_fail_survives_task_json_write_failure(self):
        self._check_state_write_failure("fail", "failed", "**Status:** KICKED OUT")

    def test_complete_survives_task_json_write_failure(self):
        self._check_state_write_failure("complete", "done", "**Status:** DONE")

    # ------------------------------------------------------------------
    # review summary write failure after a successful move
    # ------------------------------------------------------------------
    def _check_summary_write_failure(self, verb: str, where: str, status: str):
        self._break_summary_write()
        getattr(self.lifecycle, verb)(self.TASK_ID, "reason text")   # must not raise

        self._assert_moved_to(where)
        # task.json was still stamped — the steps are independent
        self.assertEqual(status, self._status_on_disk(where))
        # the failure names the path it could not write and says nothing was written
        log = self._log()
        self.assertIn(str(self.review_path), log)
        self.assertIn("no review summary was written", log)
        self.assertNotIn(f"exec summary: {self.review_path}", log)

    def test_park_survives_review_summary_write_failure(self):
        self._check_summary_write_failure("park", "parked", "parked")

    def test_fail_survives_review_summary_write_failure(self):
        self._check_summary_write_failure("fail", "failed", "failed")

    def test_complete_survives_review_summary_write_failure(self):
        self._check_summary_write_failure("complete", "done", "done")

    # ------------------------------------------------------------------
    # both writes failing is still not fatal
    # ------------------------------------------------------------------
    def test_both_bookkeeping_writes_failing_still_returns(self):
        self._break_state_write()
        self._break_summary_write()
        self.lifecycle.park(self.TASK_ID, "reason text")             # must not raise

        self._assert_moved_to("parked")
        log = self._log()
        self.assertIn("was not updated", log)
        self.assertIn("no review summary was written", log)
        self.assertIn("PARKED", log)          # the transition itself is reported

    # ------------------------------------------------------------------
    # the move itself failing is fatal
    # ------------------------------------------------------------------
    def test_move_failure_propagates_and_writes_no_bookkeeping(self):
        message = "Cross-device link"

        def explode(src, dst):
            raise OSError(message)
        self._monkeypatch(tl.shutil, "move", explode)

        with self.assertRaises(OSError) as caught:
            self.lifecycle.park(self.TASK_ID, "reason text")
        self.assertIn(message, str(caught.exception))

        # nothing moved, nothing stamped, no summary anywhere
        self.assertTrue((self.queue_dir / "active" / self.TASK_ID).is_dir())
        self.assertFalse((self.queue_dir / "parked" / self.TASK_ID).exists())
        self.assertEqual("active", self._status_on_disk("active"))
        self.assertFalse(self.review_path.exists())
        self.assertNotIn("PARKED", self._log())

    def test_real_move_failure_propagates(self):
        """A destination occupied by a non-directory makes `shutil.move` raise
        for real, with no stub in the way."""
        (self.queue_dir / "parked" / self.TASK_ID).write_text("in the way")

        with self.assertRaises(OSError):
            self.lifecycle.park(self.TASK_ID, "reason text")

        self.assertTrue((self.queue_dir / "active" / self.TASK_ID).is_dir())
        self.assertEqual("active", self._status_on_disk("active"))
        self.assertFalse(self.review_path.exists())


if __name__ == "__main__":
    unittest.main()
