"""T07 — one log sink that actually writes the file, and never breaks a run.

`composition._log` and `handlers._log` were two identical `print()` functions,
so `work/logs/harness.log` was claimed by the README but written by nobody.
`LogSink` is the single implementation: echo to stdout, append the same line
timestamped to the file, rotate at `max_bytes` keeping one generation, and
degrade to echo-only (warned once) instead of raising when the disk misbehaves.
"""
from __future__ import annotations

import io
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import logsink  # noqa: E402
from harness.core.logsink import LogSink  # noqa: E402

CAP = 200
TIMESTAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}\] ")
_DEFAULT = object()   # so a test can pass path=None on purpose


class LogSinkTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t07-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = self.dir / "harness.log"

    def _sink(self, echo=False, max_bytes=CAP, path=_DEFAULT) -> LogSink:
        sink = LogSink(self.log if path is _DEFAULT else path, echo=echo,
                       max_bytes=max_bytes)
        self.addCleanup(sink.close)
        return sink

    def _write_lines(self, sink: LogSink, count: int, msg: str = "payload") -> None:
        with redirect_stdout(io.StringIO()):
            for i in range(count):
                sink(f"{msg} {i} " + "x" * 20)

    def test_writes_timestamped_records_and_echoes_plain(self):
        sink = self._sink(echo=True)
        out = io.StringIO()
        with redirect_stdout(out):
            sink("verdict: pass")
            sink()
        self.assertEqual(out.getvalue(), "verdict: pass\n\n",
                         "stdout echo must keep the original text")
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], TIMESTAMP)
        self.assertTrue(lines[0].endswith("verdict: pass"))
        self.assertRegex(lines[1], TIMESTAMP)

    def test_silent_sink_still_writes(self):
        sink = self._sink(echo=False)
        out = io.StringIO()
        with redirect_stdout(out):
            sink("quiet")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("quiet", self.log.read_text(encoding="utf-8"))

    def test_none_path_is_echo_only(self):
        sink = self._sink(echo=True, path=None)
        out = io.StringIO()
        with redirect_stdout(out):
            sink("no file target")
        self.assertEqual(out.getvalue(), "no file target\n")
        self.assertFalse(self.log.exists())

    def test_rotates_at_the_cap_keeping_one_generation(self):
        self._write_lines(self._sink(), 60)
        self.assertTrue(self.log.exists(), "current log vanished")
        archived = self.dir / "harness.log.1"
        self.assertTrue(archived.exists(), "no rotation happened")
        self.assertLessEqual(archived.stat().st_size, CAP,
                             "archived log is over the cap (rotated too late)")
        self.assertLessEqual(self.log.stat().st_size, 2 * CAP)

    def test_exactly_one_generation(self):
        self._write_lines(self._sink(), 300)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["harness.log", "harness.log.1"])

    def test_cap_is_measured_in_utf8_bytes(self):
        sink = self._sink()
        with redirect_stdout(io.StringIO()):
            for _ in range(40):
                sink("⚠ café " + "é" * 20)
        for name in (p.name for p in self.dir.iterdir()):
            (self.dir / name).read_text(encoding="utf-8")  # must decode cleanly
        self.assertLessEqual(self.log.stat().st_size, 2 * CAP)
        self.assertTrue((self.dir / "harness.log.1").exists(),
                        "multibyte records under-ran the cap")

    def test_unwritable_log_degrades_to_echo_and_warns_once(self):
        blocked = self.dir / "blocked"
        blocked.write_text("not a directory")
        sink = self._sink(echo=True, path=blocked / "harness.log")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            for _ in range(3):
                sink("still running")          # must not raise
        self.assertEqual(out.getvalue(), "still running\n" * 3)
        warnings = [ln for ln in err.getvalue().splitlines() if "WARNING" in ln]
        self.assertEqual(len(warnings), 1, f"expected one warning, got {warnings}")

    def test_failed_rotation_keeps_appending(self):
        sink = self._sink()
        err = io.StringIO()
        with mock.patch.object(logsink.os, "replace",
                               side_effect=OSError("no space left")), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            self._write_lines(sink, 40)        # must not raise
        self.assertTrue(self.log.exists(), "log vanished on a failed rotation")
        self.assertGreater(self.log.stat().st_size, CAP, "did not keep appending")
        self.assertFalse((self.dir / "harness.log.1").exists())
        warnings = [ln for ln in err.getvalue().splitlines() if "WARNING" in ln]
        self.assertEqual(len(warnings), 1, f"expected one warning, got {warnings}")

    def test_status_on_tty_writes_in_place_without_file_record(self):
        sink = LogSink(self.log, echo=True, force_tty=True)
        self.addCleanup(sink.close)
        out = io.StringIO()
        with redirect_stdout(out):
            sink.status("working on slice 1.1")
        self.assertEqual(out.getvalue(), "\r\033[Kworking on slice 1.1")
        self.assertFalse(self.log.exists(), "status line must not be written to log file")

    def test_status_on_non_tty_is_silent_noop(self):
        sink = LogSink(self.log, echo=True, force_tty=False)
        self.addCleanup(sink.close)
        out = io.StringIO()
        with redirect_stdout(out):
            sink.status("working on slice 1.1")
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(self.log.exists())

    def test_log_line_clears_active_statusline_on_tty(self):
        sink = LogSink(self.log, echo=True, force_tty=True)
        self.addCleanup(sink.close)
        out = io.StringIO()
        with redirect_stdout(out):
            sink.status("transient spinner")
            sink("permanent log line")
        self.assertEqual(out.getvalue(), "\r\033[Ktransient spinner\r\033[Kpermanent log line\n")
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("permanent log line"))

    def test_clear_status_erases_line_on_tty(self):
        sink = LogSink(self.log, echo=True, force_tty=True)
        self.addCleanup(sink.close)
        out = io.StringIO()
        with redirect_stdout(out):
            sink.status("transient spinner")
            sink.clear_status()
        self.assertEqual(out.getvalue(), "\r\033[Ktransient spinner\r\033[K")
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
