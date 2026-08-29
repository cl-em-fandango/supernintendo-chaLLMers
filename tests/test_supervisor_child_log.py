"""T08 — supervised child output lands in a capped per-spawn file, not /dev/null.

`ChildTracker.spawn` used to run the child with `stdout=DEVNULL, stderr=DEVNULL`,
so every verdict line, heartbeat, warning and traceback from the supervised
harness vanished and a parked task left no record of why. Child stdout and
stderr now share one file handle under `<WORK_DIR>/logs/children/` (same fd
keeps their relative order), delimited by spawn/exit banner lines, and the
directory is capped at `MAX_CHILD_LOGS` files with the oldest deleted first.

The card also binds the loop's two call sites: their `label` is the
*subcommand* (`status`, `run-task-loop`, `autonomous`) — the thing a human
would rerun and therefore tail — not the internal `CycleAction` value that
picked it.
"""
from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402
from harness.workflow.cycle import (CycleAction, command_for_action,  # noqa: E402
                                    subcommand_for_action)


class SupervisorChildLogTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t08-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for patch in (
            mock.patch.object(S, "WORK_DIR", self.dir),
            mock.patch.object(S, "LOG", self.dir / "supervisor.log"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.children = self.dir / "logs" / "children"

    def _spawn(self, code: str, label: str = "probe") -> int:
        """spawn() logs a line per call; swallow its echo so output stays readable."""
        with redirect_stdout(io.StringIO()):
            return S.ChildTracker().spawn([sys.executable, "-c", code],
                                          label=label)

    def _child_files(self, label: str = "*") -> list[Path]:
        return sorted(self.children.glob(f"*{label}*"))

    def test_captures_stdout_and_stderr_between_banners(self):
        rc = self._spawn("print('child-out'); "
                         "import sys; print('child-err', file=sys.stderr)")
        self.assertEqual(rc, 0)
        files = self._child_files("probe")
        self.assertEqual(len(files), 1, files)
        txt = files[0].read_text(encoding="utf-8")
        self.assertIn("child-out", txt)
        self.assertIn("child-err", txt)
        self.assertIn("=== spawn probe args=", txt)
        self.assertIn("=== exited rc=0 ===", txt)
        self.assertIn("probe", files[0].name)

    def test_nonzero_rc_is_in_the_exit_banner(self):
        rc = self._spawn("import sys; sys.exit(3)", label="run-one")
        self.assertEqual(rc, 3)
        txt = self._child_files("run-one")[0].read_text(encoding="utf-8")
        self.assertIn("=== exited rc=3 ===", txt)

    def test_child_writes_to_its_own_process_group(self):
        # start_new_session=True must survive the DEVNULL removal: tree-kill needs it
        rc = self._spawn("import os; print(os.getpgid(0))", label="pgid")
        self.assertEqual(rc, 0)
        txt = self._child_files("pgid")[0].read_text(encoding="utf-8")
        child_pgid = int(txt.splitlines()[1])  # line 0 is the spawn banner
        self.assertNotEqual(child_pgid, os.getpgid(os.getpid()))

    def test_label_is_required(self):
        with self.assertRaises(TypeError):
            S.ChildTracker().spawn([sys.executable, "-c", "pass"])

    def test_cap_deletes_oldest_first(self):
        with mock.patch.object(S, "MAX_CHILD_LOGS", 3):
            for _ in range(7):
                self._spawn("print('x')", label="capped")
        files = self._child_files()
        self.assertLessEqual(len(files), 3,
                             f"children dir grew past the cap: {[p.name for p in files]}")
        # the survivors are the three newest (names sort chronologically)
        self.assertEqual([p.name for p in files],
                         sorted(p.name for p in files))

    def test_cap_is_env_overridable(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_MAX_CHILD_LOGS": "7"}):
            importlib.reload(S)
        try:
            self.assertEqual(S.MAX_CHILD_LOGS, 7)
        finally:
            importlib.reload(S)  # restore defaults for any later test


class LabelIsTheSubcommandTest(unittest.TestCase):
    """Every child label names the subcommand it labels (T08 item 5)."""

    def test_mapping(self):
        self.assertEqual(subcommand_for_action(CycleAction.RESUME),
                         "run-task-loop")
        self.assertEqual(subcommand_for_action(CycleAction.WORK),
                         "run-task-loop")
        self.assertEqual(subcommand_for_action(CycleAction.GENERATE),
                         "autonomous")
        # T44: a blocked cycle runs nothing, so there is nothing to label.
        self.assertEqual(subcommand_for_action(CycleAction.BLOCKED), "")

    def test_label_appears_in_the_command_it_labels(self):
        """An action with no child has no label either (T44's BLOCKED slot)."""
        for action in CycleAction:
            label = subcommand_for_action(action)
            cmd = command_for_action(action, sys.executable)
            if cmd:
                self.assertIn(label, cmd,
                              f"{action}: label {label!r} is not in {cmd}")
            else:
                self.assertEqual(label, "")

    def _loop_labels(self, *, pending: int, in_flight: int) -> list[str]:
        """Run one supervised cycle with a fake tracker; return its labels."""
        labels: list[str] = []
        claims: list[str] = []

        class _RecordingTracker:
            def spawn(self, args, *, label: str) -> int:
                labels.append(label)
                return 0

            def kill_tree(self) -> None:
                pass

        class _Provider:
            def fetch_pending(self, claim: bool = False,
                              limit: int | None = None) -> list[str]:
                return ["task"] * pending

            def list_claims(self) -> list[str]:
                return list(claims)

        with tempfile.TemporaryDirectory(prefix="t08-label-") as tmp:
            for patch in (
                mock.patch.object(S, "LOG", Path(tmp) / "supervisor.log"),
                mock.patch.object(S, "STOPFILE", Path(tmp) / "STOP"),
                mock.patch.object(S, "acquire_lock", lambda: True),
                mock.patch.object(S, "release_lock", lambda: None),
                mock.patch.object(S.signal, "signal", lambda *a, **k: None),
                mock.patch.object(S, "load", lambda path: mock.MagicMock()),
                mock.patch.object(S, "create_provider", lambda cfg: _Provider()),
                mock.patch.object(S, "TaskLifecycle", lambda cfg, log=None: None),
                mock.patch.object(S, "in_flight_task_dirs",
                                  lambda lifecycle: ["t"] * in_flight),
                mock.patch.object(S, "ChildTracker", _RecordingTracker),
                mock.patch.object(S, "_sleep", lambda stop, seconds: None),
                mock.patch.object(S, "MAX_CYCLES", 1),
            ):
                patch.start()
                self.addCleanup(patch.stop)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(S.run_loop(), 0)
        return labels

    def test_probe_is_labelled_status_and_work_names_its_subcommand(self):
        self.assertEqual(self._loop_labels(pending=1, in_flight=0),
                         ["status", "run-task-loop"])

    def test_in_flight_resumes_under_the_same_subcommand_label(self):
        """RESUME and WORK share a child, so they share its log label."""
        self.assertEqual(self._loop_labels(pending=0, in_flight=1),
                         ["status", "run-task-loop"])

    def test_an_empty_queue_labels_the_generator(self):
        self.assertEqual(self._loop_labels(pending=0, in_flight=0),
                         ["status", "autonomous"])


if __name__ == "__main__":
    unittest.main()
