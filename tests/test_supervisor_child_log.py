"""T08 — supervised child output lands in a capped per-spawn file, not /dev/null.

`ChildTracker.spawn` used to run the child with `stdout=DEVNULL, stderr=DEVNULL`,
so every verdict line, heartbeat, warning and traceback from the supervised
harness vanished and a parked task left no record of why. Child stdout and
stderr now share one file handle under `<WORK_DIR>/logs/children/` (same fd
keeps their relative order), delimited by spawn/exit banner lines, and the
directory is capped at `MAX_CHILD_LOGS` files with the oldest deleted first.
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


if __name__ == "__main__":
    unittest.main()
