"""Slice-5 tests: quick mode — `interrupt` without `--stand-down` (FR-2).

The full borrow-the-model lifecycle, verified in-process:

* 5.1 up-front validation *before any file write*: unknown `--model`,
  pool-valued `--model` (FR-2.3/E7), an already-active interrupt (FR-2.1/E2)
  and a missing TTY without `--prompt` (FR-2.4) all fail non-zero with no
  state written.
* 5.2 pause-wait → spawn: after the harness pauses, the quick `pi` session
  runs through `external/pi_cli.py` mechanics with inherited stdio, the
  selected model argument, one-shot (`--prompt`) vs interactive, and the
  requester pid on the file.
* 5.3 cleanup: session exit (any code) logs `resuming` and deletes the file
  (auto-resume); a wait timeout cancels the request; a killed requester
  leaves the file in place for `harness.py resume` (FR-2.5/E3).

Every fixture is a temp dir with `build()` patched and a fake `pi`
executable on PATH recording its argv — no containers, no real `pi`, no
`/srv/pi-harness` writes (C2/C3).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
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

MODELS = {
    "technicalWriter": "WriterModel",
    "implementer": "CoderModel",
    "fastPool": ["FastA", "FastB"],
    "randomPool": ["RandA"],
}
MODEL_CONTEXT = {"WriterModel": 131072, "CoderModel": 131072,
                 "OtherKnownModel": 32768}

FAKE_PI_SOURCE = """#!{python}
import json, os, sys
rec = os.environ.get("FAKE_PI_RECORD")
if rec:
    with open(rec, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(int(os.environ.get("FAKE_PI_RC", "0")))
"""


class _TTY:
    """Stand-in for an attached terminal."""

    def isatty(self) -> bool:
        return True


class _QuickFixture(unittest.TestCase):
    """Temp dir, `build()` patched, fake `pi` on PATH, stdio captured."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-quick-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.messages: list[str] = []
        cfg = types.SimpleNamespace(
            harness_execution_and_queue_dir=self.dir, session_timeout=SESSION_TIMEOUT_S,
            logs_dir=self.dir / "logs", repo_dir=self.dir,
            models=dict(MODELS), model_context_map=dict(MODEL_CONTEXT),
            configured_models=["WriterModel", "CoderModel", "FastA",
                              "FastB", "RandA"])
        wired = (cfg, None, None, None, None,
                 lambda line="": self.messages.append(str(line)))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)
        out = mock.patch.object(sys, "stdout", new_callable=StringIO)
        self.stdout: StringIO = out.start()
        self.addCleanup(out.stop)
        err = mock.patch.object(sys, "stderr", new_callable=StringIO)
        self.stderr: StringIO = err.start()
        self.addCleanup(err.stop)
        # No TTY by default: prompt-less calls must fail the FR-2.4 gate.
        stdin = mock.patch.object(sys, "stdin", new_callable=StringIO)
        self.stdin: StringIO = stdin.start()
        self.addCleanup(stdin.stop)
        self._install_fake_pi()

    def _install_fake_pi(self) -> None:
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "pi"
        fake.write_text(FAKE_PI_SOURCE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.record = self.dir / "pi-argv.jsonl"
        env = mock.patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
             "FAKE_PI_RECORD": str(self.record)})
        env.start()
        self.addCleanup(env.stop)

    def _logged(self) -> str:
        return " | ".join(self.messages)

    def _invocations(self) -> list[list[str]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in
                self.record.read_text().splitlines()]

    def _file_bytes(self) -> bytes | None:
        path = interrupt.interrupt_path(self.dir)
        return path.read_bytes() if path.exists() else None

    def _acknowledger(self, delay_s: float) -> None:
        """Stand in for a run loop reaching a session boundary: after
        `delay_s`, acknowledge the request (`requested -> paused`)."""
        def ack() -> None:
            time.sleep(delay_s)
            interrupt.acknowledge_interrupt(self.dir)

        thread = threading.Thread(target=ack, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

    def _quick(self, **kwargs) -> int:
        kwargs.setdefault("poll_interval", 0.02)
        kwargs.setdefault("timeout", 5.0)
        return handlers.cmd_interrupt(stand_down=False, **kwargs)


class TestQuickValidation(_QuickFixture):
    """5.1: every refused quick request fails before any file is written."""

    def test_unknown_model_rejected_without_writing(self):
        rc = self._quick(model="NoSuchModel", prompt="hi")
        self.assertEqual(rc, 1)
        self.assertIsNone(self._file_bytes())
        self.assertIn("unknown model", self._logged())
        self.assertEqual(self._invocations(), [])

    def test_pool_valued_model_rejected_without_writing(self):
        for pool in ("fastPool", "randomPool"):
            with self.subTest(pool=pool):
                rc = self._quick(model=pool, prompt="hi")
                self.assertEqual(rc, 1)
                self.assertIsNone(self._file_bytes())
                self.assertIn("pool", self._logged())
        self.assertEqual(self._invocations(), [])

    def test_missing_default_model_rejected_without_writing(self):
        cfg_models = handlers.build()[0].models
        cfg_models.pop("technicalWriter")
        rc = self._quick(prompt="hi")
        self.assertEqual(rc, 1)
        self.assertIsNone(self._file_bytes())
        self.assertIn("technicalWriter", self._logged())

    def test_active_interrupt_refused_without_touching_file(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.PAUSED)
        before = self._file_bytes()
        rc = self._quick(prompt="hi")
        self.assertEqual(rc, 1)
        self.assertEqual(self._file_bytes(), before)
        self.assertIn("already active", self._logged())
        self.assertEqual(self._invocations(), [])

    def test_no_tty_without_prompt_rejected_without_writing(self):
        rc = self._quick()
        self.assertEqual(rc, 1)
        self.assertIsNone(self._file_bytes())
        logged = self._logged()
        self.assertIn("scripts/harness-run", logged)
        self.assertIn("--prompt", logged)
        self.assertEqual(self._invocations(), [])

    def test_resolve_quick_model_accepts_configured_names(self):
        cfg = handlers.build()[0]
        quiet = lambda line="": None  # noqa: E731
        self.assertEqual(handlers._resolve_quick_model(cfg, None, quiet),
                         "WriterModel")
        self.assertEqual(
            handlers._resolve_quick_model(cfg, "technicalWriter", quiet),
            "WriterModel")
        self.assertEqual(
            handlers._resolve_quick_model(cfg, "implementer", quiet),
            "CoderModel")
        self.assertEqual(
            handlers._resolve_quick_model(cfg, "OtherKnownModel", quiet),
            "OtherKnownModel")
        self.assertIsNone(handlers._resolve_quick_model(cfg, "nope", quiet))


class TestQuickSession(_QuickFixture):
    """5.2: pause-wait → fake-`pi` session with the selected model."""

    def test_one_shot_happy_path_auto_resumes(self):
        self._acknowledger(0.05)
        rc = self._quick(model="WriterModel", prompt="hello world")
        self.assertEqual(rc, 0)
        argv = self._invocations()
        self.assertEqual(len(argv), 1)
        self.assertIn("--model", argv[0])
        self.assertEqual(argv[0][argv[0].index("--model") + 1], "WriterModel")
        self.assertIn("-p", argv[0])
        self.assertEqual(argv[0][argv[0].index("-p") + 1], "hello world")
        self.assertIn("--no-session", argv[0])
        self.assertIsNone(self._file_bytes())
        self.assertIn(handlers.QUICK_RESUMING_LOG, self._logged())
        self.assertIn("resumes", self.stdout.getvalue())

    def test_default_model_is_technical_writer(self):
        self._acknowledger(0.05)
        rc = self._quick(prompt="hi")
        self.assertEqual(rc, 0)
        argv = self._invocations()[0]
        self.assertEqual(argv[argv.index("--model") + 1], "WriterModel")

    def test_interactive_session_gets_no_prompt_arg(self):
        self._acknowledger(0.05)
        with mock.patch.object(sys, "stdin", new=_TTY()):
            rc = self._quick(model="CoderModel")
        self.assertEqual(rc, 0)
        argv = self._invocations()[0]
        self.assertEqual(argv[argv.index("--model") + 1], "CoderModel")
        self.assertNotIn("-p", argv)
        self.assertNotIn("--no-session", argv)

    def test_request_carries_pid_and_pauses_before_spawn(self):
        """At spawn time the file is QUICK/PAUSED with our requester pid."""
        seen: dict = {}

        def spy(**kwargs):
            status = interrupt.read_interrupt(self.dir)
            seen["status"] = status
            return 0

        with mock.patch.object(handlers, "run_quick_pi_session", spy):
            self._acknowledger(0.05)
            rc = self._quick(prompt="hi")
        self.assertEqual(rc, 0)
        status = seen["status"]
        self.assertIs(status.mode, interrupt.InterruptMode.QUICK)
        self.assertIs(status.state, interrupt.InterruptState.PAUSED)
        self.assertEqual(status.requester_pid, os.getpid())

    def test_session_nonzero_exit_still_resumes(self):
        """Any session exit code removes the file (FR-2.5)."""
        os.environ["FAKE_PI_RC"] = "3"
        self._acknowledger(0.05)
        rc = self._quick(prompt="hi")
        self.assertEqual(rc, 0)
        self.assertIsNone(self._file_bytes())
        self.assertIn("rc=3", self._logged())
        self.assertIn(handlers.QUICK_RESUMING_LOG, self._logged())


class TestQuickCleanup(_QuickFixture):
    """5.3: timeout-cancel, cleared-during-wait, killed requester."""

    def test_timeout_cancels_request(self):
        """No acknowledger: the request is cancelled, never spawned (FR-2.2)."""
        rc = self._quick(prompt="hi", timeout=0.2)
        self.assertEqual(rc, 1)
        self.assertIsNone(self._file_bytes())
        self.assertIn("timed out", self._logged())
        self.assertIn(handlers.QUICK_RESUMING_LOG, self._logged())
        self.assertEqual(self._invocations(), [])

    def test_request_written_requested_before_wait(self):
        """The written record is QUICK/REQUESTED with the requester pid."""
        seen: dict = {}

        def fake_wait(work_dir, timeout, poll_interval=None):
            seen["status"] = interrupt.read_interrupt(work_dir)
            return handlers.StandDownWaitResult.TIMED_OUT

        with mock.patch.object(handlers, "wait_for_paused", fake_wait):
            rc = self._quick(prompt="hi")
        self.assertEqual(rc, 1)
        status = seen["status"]
        self.assertIs(status.mode, interrupt.InterruptMode.QUICK)
        self.assertIs(status.state, interrupt.InterruptState.REQUESTED)
        self.assertEqual(status.requester_pid, os.getpid())

    def test_cleared_during_wait_starts_no_session(self):
        """A `resume` mid-wait: no borrow, non-zero, file stays gone."""
        def clearer() -> None:
            time.sleep(0.05)
            interrupt.clear_interrupt(self.dir)

        thread = threading.Thread(target=clearer, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        rc = self._quick(prompt="hi", timeout=5.0)
        self.assertEqual(rc, 1)
        self.assertEqual(self._invocations(), [])
        self.assertIsNone(self._file_bytes())
        self.assertIn("cleared before the harness paused", self._logged())

    def test_killed_requester_leaves_file_for_resume(self):
        """E3/FR-2.5: killed before cleanup → file remains (fail-safe)."""
        pid = os.fork()
        if pid == 0:  # child: a requester that dies inside the pi session
            try:
                with mock.patch.object(
                        handlers, "wait_for_paused",
                        lambda *a, **k: handlers.StandDownWaitResult.PAUSED), \
                        mock.patch.object(
                            handlers, "run_quick_pi_session",
                            lambda **kw: os._exit(9)):
                    handlers.cmd_interrupt(stand_down=False, prompt="hi",
                                           timeout=5.0, poll_interval=0.02)
                os._exit(3)
            except BaseException:
                os._exit(4)
        os.waitpid(pid, 0)
        status = interrupt.read_interrupt(self.dir)
        self.assertIsNotNone(status)
        self.assertIs(status.mode, interrupt.InterruptMode.QUICK)
        self.assertEqual(self._invocations(), [])
        # Recovery: the documented path is `harness.py resume`.
        self.assertEqual(handlers._resume_clear_interrupt(), 0)
        self.assertIsNone(self._file_bytes())


if __name__ == "__main__":
    unittest.main()
