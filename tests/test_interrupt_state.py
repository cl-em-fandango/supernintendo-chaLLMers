"""Slice-1 tests: the interrupt state file and its two CLI ends.

`harness/core/interrupt.py` owns `<harnessExecutionAndQueueDir>/state/interrupt.json`: the
dataclass + Enums, the atomic write, the fail-safe read, the delete and the
age helper. Slice 1 wires the two commands that only touch the file —
`interrupt --stand-down` (write, idempotent) and no-arg `resume` (clear) —
and this file tests both the module and those handlers at the handler edge.

Covered here:
  * write→read round-trip at `<harnessExecutionAndQueueDir>/state/interrupt.json`, values as the
    Enum wire values, timestamps UTC ISO-8601 (FR-5.1/FR-5.2);
  * read of a missing file is "no interrupt" (None) (FR-5.2);
  * a corrupt file reads as STAND_DOWN/REQUESTED and logs the recovery hint
    naming `harness.py resume` (E5/FR-5.3);
  * writes are atomic: a simulated crash during the rename leaves the prior
    file byte-identical and no temp litter, and creates nothing when there
    was no prior file (FR-5.3);
  * `cmd_interrupt(--stand-down, --no-wait)` writes REQUESTED and returns 0,
    works with no harness running (FR-1.1/FR-1.4), and a second request
    changes nothing on disk but the log (E1); the non-stand-down form is
    refused with no file written (quick mode arrives in Slice 5). The
    wait/timeout semantics of the default form live in
    `tests/test_interrupt_wait.py` (Slice 4);
  * no-arg `cmd_resume` deletes the file, logs the interruption duration and
    returns 0; with no file it prints `no interrupt active` and returns 0
    (FR-3.2, AC3 partial).

Every fixture is a temp dir with `build()` patched — no containers, no real
`pi`, no `/srv/pi-harness` writes (C2/C3).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.cli.parser import parse_args  # noqa: E402
from harness.core import interrupt  # noqa: E402


class _TempWorkDir(unittest.TestCase):
    """Shared temp dir; nothing here resolves config.json."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestStateFile(_TempWorkDir):
    """The module: path, round-trip, fail-safe read, atomicity, age."""

    def test_path_is_state_interrupt_json(self):
        self.assertEqual(interrupt.interrupt_path(self.dir),
                         self.dir / "state" / "interrupt.json")

    def test_write_read_round_trip(self):
        written = interrupt.write_interrupt(self.dir, interrupt.InterruptMode.QUICK,
                                            interrupt.InterruptState.REQUESTED,
                                            requester_pid=4242)
        raw = json.loads(interrupt.interrupt_path(self.dir).read_text())
        self.assertEqual(raw["mode"], "quick")
        self.assertEqual(raw["state"], "requested")
        self.assertEqual(raw["requester_pid"], 4242)
        # UTC ISO-8601 at the file edge; Enums inside the code.
        parsed = datetime.fromisoformat(raw["requested_at"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(written, interrupt.read_interrupt(self.dir))

    def test_read_missing_file_is_no_interrupt(self):
        self.assertIsNone(interrupt.read_interrupt(self.dir))

    def test_corrupt_file_reads_as_stand_down_requested_with_hint(self):
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")
        warnings: list[str] = []
        status = interrupt.read_interrupt(self.dir, log=warnings.append)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.REQUESTED)
        self.assertEqual(len(warnings), 1)
        self.assertIn("harness.py resume", warnings[0])

    def test_unknown_enum_values_are_corrupt_too(self):
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mode": "resuming", "state": "paused",
                                    "requested_at": "x", "updated_at": "y"}))
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.REQUESTED)

    def test_crash_during_rename_leaves_prior_file_intact(self):
        first = interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                          interrupt.InterruptState.PAUSED)
        before = interrupt.interrupt_path(self.dir).read_bytes()
        with mock.patch.object(interrupt.os, "replace",
                               side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                interrupt.write_interrupt(self.dir, interrupt.InterruptMode.QUICK,
                                          interrupt.InterruptState.REQUESTED)
        self.assertEqual(interrupt.interrupt_path(self.dir).read_bytes(), before)
        self.assertEqual(interrupt.read_interrupt(self.dir), first)
        leftovers = [p.name for p in (self.dir / "state").iterdir()
                     if p.name != "interrupt.json"]
        self.assertEqual(leftovers, [])

    def test_crash_before_any_file_creates_nothing(self):
        with mock.patch.object(interrupt.os, "replace",
                               side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                          interrupt.InterruptState.REQUESTED)
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())
        self.assertIsNone(interrupt.read_interrupt(self.dir))

    def test_clear_interrupt_reports_whether_a_file_was_there(self):
        self.assertFalse(interrupt.clear_interrupt(self.dir))
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.REQUESTED)
        self.assertTrue(interrupt.clear_interrupt(self.dir))
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())

    def test_age_seconds_measures_since_request(self):
        status = interrupt.InterruptStatus(
            mode=interrupt.InterruptMode.STAND_DOWN,
            state=interrupt.InterruptState.PAUSED,
            requested_at=(datetime.now(timezone.utc)
                          - timedelta(seconds=90)).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat())
        self.assertAlmostEqual(interrupt.interrupt_age_seconds(status), 90,
                               delta=2)


class _HandlerFixture(_TempWorkDir):
    """`build()` patched to the temp dir; no real wiring, no real pi."""

    def setUp(self) -> None:
        super().setUp()
        self.messages: list[str] = []
        cfg = types.SimpleNamespace(
            harness_execution_and_queue_dir=self.dir,
            models={"technicalWriter": "WriterModel"},
            model_context_map={}, configured_models=["WriterModel"])
        wired = (cfg, None, None, None, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = mock.patch.object(sys, "stdout", new_callable=StringIO)
        self.stdout: StringIO = out.start()
        self.addCleanup(out.stop)
        err = mock.patch.object(sys, "stderr", new_callable=StringIO)
        self.stderr: StringIO = err.start()
        self.addCleanup(err.stop)

    def _logged(self) -> str:
        return " | ".join(self.messages)


class TestCmdInterruptStandDown(_HandlerFixture):
    """`interrupt --stand-down`: write, idempotent, refuses quick for now."""

    def test_writes_stand_down_requested_and_returns_zero(self):
        # --no-wait: the write path alone; the wait path is Slice 4's file.
        self.assertEqual(
            handlers.cmd_interrupt(stand_down=True, no_wait=True), 0)
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.REQUESTED)
        self.assertEqual(status.requester_pid, os.getpid())

    def test_second_request_changes_nothing_but_the_log(self):
        handlers.cmd_interrupt(stand_down=True, no_wait=True)
        before = interrupt.interrupt_path(self.dir).read_bytes()
        self.assertEqual(
            handlers.cmd_interrupt(stand_down=True, no_wait=True), 0)
        self.assertEqual(interrupt.interrupt_path(self.dir).read_bytes(), before)
        self.assertIn("interrupt already active", self._logged())

    def test_request_survives_with_no_harness_running(self):
        # FR-1.4: the handler only touches the file — a plain temp dir
        # *is* "no harness running".
        self.assertEqual(
            handlers.cmd_interrupt(stand_down=True, no_wait=True), 0)
        self.assertTrue(interrupt.interrupt_path(self.dir).exists())

    def test_quick_form_is_routed_not_refused_unimplemented(self):
        # Slice 5 implemented quick mode: the non-stand-down form is no
        # longer rejected as "not implemented". It routes into the quick
        # flow (tests/test_interrupt_quick.py); here, with no TTY and no
        # --prompt, the FR-2.4 gate still refuses it *before* any write.
        self.assertEqual(handlers.cmd_interrupt(stand_down=False), 1)
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())
        self.assertNotIn("not implemented", self.stderr.getvalue())
        self.assertNotIn("not implemented", self._logged())
        self.assertIn("scripts/harness-run", self._logged())


class TestCmdResumeNoArg(_HandlerFixture):
    """No-arg `resume`: clear + duration log; absent file is a 0 no-op."""

    def test_clears_file_logs_duration_and_returns_zero(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.PAUSED)
        self.assertEqual(handlers.cmd_resume(), 0)
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())
        logged = self._logged()
        self.assertIn("interrupt cleared", logged)
        self.assertIn("mode=STAND_DOWN state=PAUSED", logged)
        self.assertIn("duration=", logged)

    def test_no_interrupt_active_prints_and_returns_zero(self):
        self.assertEqual(handlers.cmd_resume(), 0)
        self.assertIn("no interrupt active", self.stdout.getvalue())

    def test_corrupt_file_is_cleared_by_resume(self):
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{ broken")
        self.assertEqual(handlers.cmd_resume(), 0)
        self.assertFalse(path.exists())


class TestParserSurface(unittest.TestCase):
    """The parser surface slice 1 promises; dispatch wiring is harness.py's."""

    def test_interrupt_flags_parse(self):
        args = parse_args(["interrupt", "--stand-down", "--no-wait",
                           "--timeout", "30", "--model", "writer",
                           "--prompt", "hi"])
        self.assertEqual(args.command, "interrupt")
        self.assertTrue(args.stand_down)
        self.assertTrue(args.no_wait)
        self.assertEqual(args.timeout, 30.0)
        self.assertEqual(args.model, "writer")
        self.assertEqual(args.prompt, "hi")

    def test_resume_task_id_is_optional(self):
        self.assertIsNone(parse_args(["resume"]).task_id)
        self.assertEqual(parse_args(["resume", "t1"]).task_id, "t1")


if __name__ == "__main__":
    unittest.main()
