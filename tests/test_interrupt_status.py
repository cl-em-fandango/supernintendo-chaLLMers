"""Slice 6 of interrupt handling: the `status`/`board` interrupt surface (FR-4).

While an interrupt file exists, both inspection commands show its mode
(STAND_DOWN/QUICK), its state (REQUESTED/PAUSED) and its age since
`requested_at`; when no file exists they print nothing extra (FR-4.1). Both
must keep exiting 0 while an interrupt is active, because the supervisor's
circuit-breaker probe is `harness.py status` (FR-6.3). A corrupt file reads
fail-safe as STAND_DOWN/REQUESTED and the surface shows that record plus the
recovery-hint warning (FR-5.3, E5).

Every fixture is a temp dir with `build()` patched (the `_WiredFixture`
pattern from test_handlers_claims.py), so the real work tree is never opened
and nothing is written under /srv/pi-harness.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core.interrupt import (  # noqa: E402
    CORRUPT_INTERRUPT_WARNING,
    InterruptMode,
    InterruptState,
    clear_interrupt,
    interrupt_path,
    write_interrupt,
)
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402


class _WiredFixture(unittest.TestCase):
    """Temp workDir with a real state dir, provider and stats store, `build()` wired."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-status-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "queue" / "pending"
        self.claimed = self.dir / "queue" / "claimed"
        self.pending.mkdir(parents=True)
        self.claimed.mkdir(parents=True)
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)
        cfg = types.SimpleNamespace(work_dir=self.dir,
                                    queue_dir=self.dir / "queue",
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, self.provider, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"COLUMNS": "110"})
        env.start()
        self.addCleanup(env.stop)

    def _request(self, mode: InterruptMode, state: InterruptState,
                 *, age_seconds: float = 0.0) -> None:
        """Plant an interrupt record, optionally already `age_seconds` old."""
        status = write_interrupt(self.dir, mode, state)
        if age_seconds:
            requested = (datetime.now(timezone.utc)
                         - timedelta(seconds=age_seconds))
            raw = json.loads(interrupt_path(self.dir).read_text())
            raw["requested_at"] = requested.isoformat()
            interrupt_path(self.dir).write_text(json.dumps(raw))

    def _corrupt(self) -> None:
        interrupt_path(self.dir).parent.mkdir(parents=True, exist_ok=True)
        interrupt_path(self.dir).write_text("{ not json")

    def _status_output(self) -> int:
        """Run `cmd_status`; the logged lines are the output. Returns rc."""
        rc = handlers.cmd_status()
        return rc

    def _board_output(self) -> str:
        """Run `cmd_board`, return what it printed (asserts exit 0)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = handlers.cmd_board()
        self.assertEqual(rc, 0)
        return buf.getvalue()


class StatusInterruptSurfaceTest(_WiredFixture):
    """`status` shows mode/state/age while active and nothing when not."""

    def test_no_interrupt_prints_no_interrupt_line(self):
        self.assertEqual(self._status_output(), 0)
        self.assertNotIn("interrupt", self._joined())

    def test_a_stand_down_request_shows_mode_state_and_age(self):
        self._request(InterruptMode.STAND_DOWN, InterruptState.REQUESTED,
                      age_seconds=42)
        self.assertEqual(self._status_output(), 0)
        line = self._interrupt_line()
        self.assertIn("STAND_DOWN", line)
        self.assertIn("REQUESTED", line)
        self.assertIn("42s", line)

    def test_a_paused_quick_interrupt_shows_quick_and_paused(self):
        self._request(InterruptMode.QUICK, InterruptState.PAUSED,
                      age_seconds=90)
        self.assertEqual(self._status_output(), 0)
        line = self._interrupt_line()
        self.assertIn("QUICK", line)
        self.assertIn("PAUSED", line)
        self.assertIn("1m30s", line)

    def test_the_age_rolls_up_to_hours(self):
        self._request(InterruptMode.STAND_DOWN, InterruptState.PAUSED,
                      age_seconds=7325)
        self._status_output()
        self.assertIn("2h02m", self._interrupt_line())

    def test_status_exits_zero_while_an_interrupt_is_active(self):
        """FR-6.3: the breaker probe must not trip on an interrupt."""
        self._request(InterruptMode.STAND_DOWN, InterruptState.REQUESTED)
        self.assertEqual(self._status_output(), 0)

    def test_after_resume_the_interrupt_lines_are_gone(self):
        """AC5: shown while paused, hidden after `resume` clears the file."""
        self._request(InterruptMode.STAND_DOWN, InterruptState.PAUSED)
        self._status_output()
        self.assertIn("PAUSED", self._interrupt_line())
        self.messages.clear()
        self.assertTrue(clear_interrupt(self.dir))
        self.assertEqual(self._status_output(), 0)
        self.assertNotIn("interrupt", self._joined())

    def test_a_corrupt_file_shows_stand_down_requested_with_the_hint(self):
        """E5/FR-5.3: fail-safe record plus the recovery warning, exit 0."""
        self._corrupt()
        self.assertEqual(self._status_output(), 0)
        joined = self._joined()
        self.assertIn(CORRUPT_INTERRUPT_WARNING, joined)
        line = self._interrupt_line()
        self.assertIn("STAND_DOWN", line)
        self.assertIn("REQUESTED", line)

    def _joined(self) -> str:
        return " | ".join(self.messages)

    def _interrupt_line(self) -> str:
        lines = [m for m in self.messages if m.startswith("interrupt:")]
        self.assertEqual(len(lines), 1, f"expected one interrupt line, got {self.messages}")
        return lines[0]


class BoardInterruptSurfaceTest(_WiredFixture):
    """`board` shows the same facts on stdout and stays read-only."""

    def test_no_interrupt_prints_no_interrupt_line(self):
        self.assertNotIn("interrupt", self._board_output())

    def test_an_active_interrupt_shows_mode_state_and_age(self):
        self._request(InterruptMode.STAND_DOWN, InterruptState.PAUSED,
                      age_seconds=125)
        out = self._board_output()
        self.assertIn("STAND_DOWN", out)
        self.assertIn("PAUSED", out)
        self.assertIn("2m05s", out)

    def test_a_quick_request_shows_quick_and_requested(self):
        self._request(InterruptMode.QUICK, InterruptState.REQUESTED)
        out = self._board_output()
        self.assertIn("QUICK", out)
        self.assertIn("REQUESTED", out)

    def test_after_clear_the_board_prints_no_interrupt_line(self):
        self._request(InterruptMode.QUICK, InterruptState.PAUSED)
        self.assertIn("PAUSED", self._board_output())
        clear_interrupt(self.dir)
        self.assertNotIn("interrupt", self._board_output())

    def test_a_corrupt_file_shows_the_fail_safe_record_and_the_hint(self):
        self._corrupt()
        out = self._board_output()
        self.assertIn(CORRUPT_INTERRUPT_WARNING, out)
        self.assertIn("STAND_DOWN", out)
        self.assertIn("REQUESTED", out)

    def test_the_board_stays_read_only_while_an_interrupt_is_active(self):
        """FR-8: showing the interrupt moves nothing and rewrites nothing."""
        self._request(InterruptMode.STAND_DOWN, InterruptState.REQUESTED)
        before = interrupt_path(self.dir).read_bytes()
        self._board_output()
        self.assertEqual(interrupt_path(self.dir).read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.pending.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
