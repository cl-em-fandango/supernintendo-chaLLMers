"""T02 — `supervisor.log` is bounded: rotate at the cap, one generation, no crash.

`supervisor.log()` used to append forever, so a chatty loop could fill the disk
and take the supervisor (and every pi session) down with it. Records are now
formatted and encoded first, the file is rotated aside *before* the append that
would cross `MAX_LOG_BYTES`, and a rotation failure is a once-per-process
warning rather than an exception out of the loop.
"""
from __future__ import annotations

import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402

CAP = 200


class SupervisorLogRotationTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t02-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = self.dir / "supervisor.log"
        for patch in (
            mock.patch.object(S, "LOG", self.log),
            mock.patch.object(S, "MAX_LOG_BYTES", CAP),
            mock.patch.object(S, "_rotation_warned", False),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _write_lines(self, count: int, msg: str = "payload") -> None:
        """log() prints every record; swallow it so pytest output stays readable."""
        with redirect_stdout(io.StringIO()):
            for i in range(count):
                S.log(f"{msg} {i} " + "x" * 20)

    def _generations(self) -> list[str]:
        return sorted(p.name for p in self.dir.iterdir())

    def test_rotates_before_the_overflowing_append(self):
        self._write_lines(60)
        self.assertTrue(self.log.exists(), "current log vanished")
        archived = self.dir / "supervisor.log.1"
        self.assertTrue(archived.exists(), "no rotation happened")
        self.assertLessEqual(self.log.stat().st_size, 2 * CAP,
                             f"current log too big: {self.log.stat().st_size}")
        # rotation happens ahead of the append, so the archive never exceeds the cap
        self.assertLessEqual(archived.stat().st_size, CAP,
                             "archived log is over the cap (rotated too late)")

    def test_exactly_one_generation(self):
        self._write_lines(300)
        self.assertEqual(self._generations(), ["supervisor.log", "supervisor.log.1"])

    def test_cap_is_measured_in_utf8_bytes(self):
        # '⚠' and 'ó' are multi-byte: char counts would under-run the cap
        with redirect_stdout(io.StringIO()):
            for i in range(40):
                S.log("⚠ café " + "é" * 20)
        self.assertLessEqual(self.log.stat().st_size, 2 * CAP)
        for name in self._generations():
            (self.dir / name).read_text(encoding="utf-8")  # must decode cleanly

    def test_rotation_failure_keeps_appending_and_warns_once(self):
        with mock.patch.object(S.os, "replace", side_effect=OSError("no space left")):
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self._write_lines(40)          # must not raise
        self.assertTrue(self.log.exists(), "log vanished on a failed rotation")
        self.assertGreater(self.log.stat().st_size, CAP, "did not keep appending")
        self.assertFalse((self.dir / "supervisor.log.1").exists())
        warnings = [ln for ln in err.getvalue().splitlines() if "WARNING" in ln]
        self.assertEqual(len(warnings), 1, f"expected one warning, got {warnings}")

    def test_cap_is_env_overridable(self):
        with mock.patch.dict(os.environ, {"SUPERVISOR_MAX_LOG_BYTES": "1234"}):
            importlib.reload(S)
        self.assertEqual(S.MAX_LOG_BYTES, 1234)


if __name__ == "__main__":
    unittest.main()
