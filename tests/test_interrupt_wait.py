"""Slice-4 tests: `interrupt --stand-down` wait / timeout semantics (FR-1.2/FR-1.3).

After the request file is written, the command by default polls the state
file until the harness acknowledges (`state=paused`), up to
`sessionTimeout + 60` from config (`--timeout N` overrides, `--no-wait` skips
the wait entirely). Success prints the exact stand-down line and exits 0; a
timeout prints the running session's log pointer, exits non-zero and leaves
the request in place — the harness still stands down at its next boundary.

Every fixture is a temp workDir with `build()` patched and a background
acknowledger thread flipping the file to `paused` where a live harness would
— no containers, no real `pi`, no `/srv/pi-harness` writes (C2/C3).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core import interrupt  # noqa: E402

SESSION_TIMEOUT_S = 3600


class _WaitFixture(unittest.TestCase):
    """Temp workDir, `build()` patched, stdout/stderr captured."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-wait-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.messages: list[str] = []
        cfg = types.SimpleNamespace(work_dir=self.dir,
                                    session_timeout=SESSION_TIMEOUT_S,
                                    logs_dir=self.dir / "logs")
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

    def _acknowledger(self, delay_s: float) -> None:
        """Stand in for a run loop reaching a session boundary: after
        `delay_s`, acknowledge the request (`requested -> paused`)."""
        def ack() -> None:
            time.sleep(delay_s)
            interrupt.acknowledge_interrupt(self.dir)

        thread = threading.Thread(target=ack, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)


class TestWaitForPaused(unittest.TestCase):
    """The polling helper itself: the three ways a wait can end."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-wait-unit-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_no_file_is_cleared_not_paused(self):
        self.assertEqual(handlers.wait_for_paused(self.dir, 0.2,
                                                  poll_interval=0.05),
                         handlers.StandDownWaitResult.CLEARED)

    def test_pending_request_times_out(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.REQUESTED)
        started = time.monotonic()
        result = handlers.wait_for_paused(self.dir, 0.3, poll_interval=0.05)
        self.assertEqual(result, handlers.StandDownWaitResult.TIMED_OUT)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_zero_timeout_reports_immediately(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.REQUESTED)
        self.assertEqual(handlers.wait_for_paused(self.dir, 0.0),
                         handlers.StandDownWaitResult.TIMED_OUT)

    def test_paused_file_returns_paused(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.PAUSED)
        self.assertEqual(handlers.wait_for_paused(self.dir, 0.2),
                         handlers.StandDownWaitResult.PAUSED)


class TestStandDownWait(_WaitFixture):
    """`interrupt --stand-down` blocking semantics (FR-1.2/FR-1.3)."""

    def test_wait_returns_zero_with_the_exact_success_line(self):
        self._acknowledger(delay_s=0.15)
        rc = handlers.cmd_interrupt(stand_down=True, timeout=5.0,
                                    poll_interval=0.05)
        self.assertEqual(rc, 0)
        self.assertIn("harness stood down — model released "
                      "(task(s) left in active/ at checkpoints)",
                      self.stdout.getvalue())
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)

    def test_no_wait_exits_zero_immediately_after_write(self):
        # No acknowledger exists: only --no-wait can return this fast.
        started = time.monotonic()
        rc = handlers.cmd_interrupt(stand_down=True, no_wait=True)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 1.0)
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.REQUESTED)
        self.assertNotIn("harness stood down", self.stdout.getvalue())

    def test_timeout_exits_nonzero_prints_log_pointer_keeps_request(self):
        # FR-1.3 + FR-1.4: nothing acknowledges (no harness running), so the
        # wait times out; the request must stay in place for the next start.
        rc = handlers.cmd_interrupt(stand_down=True, timeout=0.3,
                                    poll_interval=0.05)
        self.assertNotEqual(rc, 0)
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.REQUESTED)
        logged = self._logged()
        self.assertIn("timed out", logged)
        self.assertIn("request stays in place", logged)
        self.assertIn(str(self.dir / "logs" / "harness.log"), logged)
        self.assertNotIn("harness stood down", self.stdout.getvalue())

    def test_default_timeout_is_session_timeout_plus_sixty(self):
        seen: list[float] = []

        def fake_wait(work_dir, timeout, poll_interval=None):
            seen.append(timeout)
            return handlers.StandDownWaitResult.PAUSED

        with mock.patch.object(handlers, "wait_for_paused", fake_wait):
            self.assertEqual(handlers.cmd_interrupt(stand_down=True), 0)
        self.assertEqual(seen, [SESSION_TIMEOUT_S
                                + handlers.INTERRUPT_WAIT_EXTRA_S])

    def test_timeout_flag_overrides_the_default(self):
        seen: list[float] = []

        def fake_wait(work_dir, timeout, poll_interval=None):
            seen.append(timeout)
            return handlers.StandDownWaitResult.PAUSED

        with mock.patch.object(handlers, "wait_for_paused", fake_wait):
            self.assertEqual(handlers.cmd_interrupt(stand_down=True,
                                                    timeout=7.5), 0)
        self.assertEqual(seen, [7.5])

    def test_request_cleared_mid_wait_exits_nonzero(self):
        # A `resume` (or quick-mode completion) removes the file: the
        # stand-down we asked for will never happen — say so, exit non-zero.
        def clear() -> None:
            time.sleep(0.15)
            interrupt.clear_interrupt(self.dir)

        thread = threading.Thread(target=clear, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        rc = handlers.cmd_interrupt(stand_down=True, timeout=5.0,
                                    poll_interval=0.05)
        self.assertNotEqual(rc, 0)
        self.assertIn("cleared", self._logged())
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())

    def test_already_active_request_returns_zero_without_waiting(self):
        # E1: the idempotent no-op never enters the wait — even with the
        # request still REQUESTED and no acknowledger in sight.
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.REQUESTED)
        started = time.monotonic()
        rc = handlers.cmd_interrupt(stand_down=True, timeout=3600.0)
        self.assertEqual(rc, 0)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("interrupt already active", self._logged())


if __name__ == "__main__":
    unittest.main()
