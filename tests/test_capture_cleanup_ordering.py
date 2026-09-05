"""Slice 4 (001-full-interactions-logged): FR-6 cleanup ordering.

The workdir `.pi-session-*` `.out`/`.err` capture is the only copy of the
child's stderr until a transcript holds it, so the capture may be deleted
*only after* the transcript (task-scoped or pooled) is durably on disk.
A failed transcript write must therefore leave the capture in place —
deleting it first would lose the stderr exactly in the case the audit trail
exists for. (Success-path removal and the no-transcript legacy placement
are pinned by `test_workdir_transcript_cleanup.py`.)

Runs a real `run_pi_session` subprocess driven by a fake `pi` script on
`PATH` that writes both stdout and stderr (temp dirs only — `setUp` asserts
`shutil.which` resolves inside the temp dir). The unwritable target is a
read-only `artifacts/` directory, the same simulation the pooled-write tests
use.

Run from the repo root:  python3 -m unittest tests.test_capture_cleanup_ordering
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore

ASSISTANT_TEXT = "All good.\n\nVERDICT: done"
STDERR_TEXT = "fake pi: deprecated flag\n"


def _cfg(work_dir: Path) -> Config:
    return Config(
        harness_execution_and_queue_dir=work_dir,
        token_budget=60_000,
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
        model_context_map={"m": 131_072},
    )


def _fake_pi(bin_dir: Path) -> None:
    """Executable `pi` replying on stdout and warning on stderr.

    The stderr line makes `run_pi_session` write the `<out_file>.err`
    capture, so "the capture survives the failed write" covers both files.
    """
    body = textwrap.dedent(f"""
        import json, sys
        if "--list-models" in sys.argv:
            print("m")
            sys.exit(0)
        sys.stderr.write({STDERR_TEXT!r})
        event = {{
            "type": "message_end",
            "message": {{
                "role": "assistant",
                "usage": {{"totalTokens": 42}},
                "content": [{{"type": "text", "text": {ASSISTANT_TEXT!r}}}],
            }},
        }}
        print(json.dumps(event))
    """)
    (bin_dir / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "try:\n"
        + textwrap.indent(body.strip("\n"), "    ") + "\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (bin_dir / "pi").chmod(0o755)


class CaptureCleanupOrderingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.task_id = "t1"
        self.task_dir = self.work_dir / "queue" / "active" / self.task_id
        self.task_dir.mkdir(parents=True)
        self.sessions_dir = self.task_dir / "artifacts" / "sessions"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        _fake_pi(self.bin_dir)

        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or Path(found).resolve().parent != self.bin_dir.resolve():
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          "refusing to invoke a real model")

        self.cfg = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)

    def _run(self, **kwargs):
        kwargs.setdefault("task_id", self.task_id)
        kwargs.setdefault("stage", Stage.SPEC_AUTHOR)
        return self.runner.run("m", self.work_repo, "Do the thing.", **kwargs)

    def _captures(self):
        return sorted(p.name for p in self.work_repo.glob(".pi-session-*"))

    def _make_artifacts_read_only(self, artifacts: Path) -> None:
        artifacts.mkdir(parents=True, exist_ok=True)
        artifacts.chmod(0o500)  # read-only: sessions/ cannot be created
        self.addCleanup(artifacts.chmod, 0o755)

    # ------------------------------------------------------------------
    # (c1) task-scoped write fails: capture survives, FR-3 warns, run ok
    # ------------------------------------------------------------------
    def test_failed_task_transcript_write_keeps_capture(self):
        self._make_artifacts_read_only(self.task_dir / "artifacts")
        result = self._run()
        (self.task_dir / "artifacts").chmod(0o755)  # restore for teardown
        warnings = [line for line in self.lines
                    if "transcript write failed for session" in line]
        self.assertEqual(len(warnings), 1,
                         f"FR-3 warning expected; log: {self.lines}")
        self.assertIn("001-spec_author", warnings[0])
        self.assertFalse(self.sessions_dir.exists(),
                         "no transcript may be claimed from a failed write")
        self.assertEqual(len(self._captures()), 2,
                         f"capture must survive the failed write: {self._captures()}")
        err_capture = next(p for p in self.work_repo.glob(".pi-session-*.err"))
        self.assertEqual(err_capture.read_text(), STDERR_TEXT,
                         "the only copy of stderr must stay recoverable")
        self.assertTrue(result.ok, "a failed audit write must not fail the run")
        self.assertEqual(len(self.store.all()), 1, "the stats row is still recorded")

    # ------------------------------------------------------------------
    # (c2) pooled write fails: capture survives, C4 fallback warns, run ok
    # ------------------------------------------------------------------
    def test_failed_pooled_write_keeps_capture(self):
        self._make_artifacts_read_only(self.work_dir / "artifacts")
        result = self._run(task_id=None)
        (self.work_dir / "artifacts").chmod(0o755)
        warnings = [line for line in self.lines
                    if "no pooled transcript written for stage" in line]
        self.assertEqual(len(warnings), 1,
                         f"FR-5 C4 fallback warning expected; log: {self.lines}")
        self.assertEqual(len(self._captures()), 2,
                         f"capture must survive the failed pooled write: {self._captures()}")
        err_capture = next(p for p in self.work_repo.glob(".pi-session-*.err"))
        self.assertEqual(err_capture.read_text(), STDERR_TEXT)
        self.assertTrue(result.ok, "a failed pooled write must not fail the run")

    # ------------------------------------------------------------------
    # control: successful task-scoped write removes both capture files
    # ------------------------------------------------------------------
    def test_successful_task_write_removes_capture(self):
        result = self._run()
        self.assertTrue(result.ok, f"log: {self.lines}")
        self.assertTrue((self.sessions_dir / "001-spec_author.md").is_file())
        self.assertEqual(self._captures(), [])


if __name__ == "__main__":
    unittest.main()
