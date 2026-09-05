"""Slice 1 — the `post-mortem` vertical spine (spec §3, AC1/AC6).

`harness.py post-mortem <task_id>` resolves a task across the queue and
prints a minimal Markdown report, or exits 1 with a single error line when
the task is nowhere. This file owns the CLI surface of that command and the
task-resolution edge; failure-mode classification, checkpoints and the rest
of the report arrive in later slices.

Covered here:
  * `post-mortem --help` shows the command, the required positional
    `task_id` and the `--save` flag (AC1);
  * the top-level help and `harness.py`'s usage docstring list the command
    next to `journey` (AC1);
  * an unknown task id with no directory and no telemetry rows exits 1 with
    exactly one error line naming the task id and the queue dir searched,
    and no report (AC6);
  * a parked task in a temp queue exits 0 and the report header names the
    task id with `**Status:** parked`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.cli.parser import build_parser, parse_args  # noqa: E402

_HARNESS_PY = Path(__file__).resolve().parents[1] / "harness.py"


class _TempHarness(unittest.TestCase):
    """A temp work dir wired through HARNESS_CONFIG, like the sync tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.queue_dir = self.work / "queue"
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps(
            {"harnessExecutionAndQueueDir": str(self.work)}), encoding="utf-8")
        self._env = mock.patch.dict(os.environ,
                                    {"HARNESS_CONFIG": str(cfg_path)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _run_handler(self, task_id: str, save: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers.cmd_post_mortem(task_id, save=save)
        return rc, out.getvalue()


class TestParserSurface(unittest.TestCase):
    def test_subparser_help_shows_task_id_and_save(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                self.assertRaises(SystemExit) as caught:
            parse_args(["post-mortem", "--help"])
        self.assertEqual(0, caught.exception.code)
        text = out.getvalue()
        self.assertIn("task_id", text)
        self.assertIn("--save", text)

    def test_task_id_is_required(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            parse_args(["post-mortem"])
        self.assertNotEqual(0, caught.exception.code)

    def test_parse_yields_task_id_and_save(self):
        args = parse_args(["post-mortem", "task-9", "--save"])
        self.assertEqual("post-mortem", args.command)
        self.assertEqual("task-9", args.task_id)
        self.assertTrue(args.save)

    def test_top_level_help_lists_post_mortem(self):
        text = build_parser().format_help()
        self.assertIn("post-mortem", text)

    def test_harness_docstring_lists_post_mortem_next_to_journey(self):
        doc = _HARNESS_PY.read_text(encoding="utf-8")
        journey_at = doc.index("harness.py journey ")
        postmortem_at = doc.index("harness.py post-mortem ")
        dispatch_at = doc.index('"post-mortem"')
        self.assertTrue(0 < journey_at < postmortem_at < dispatch_at,
                        "post-mortem usage line must sit next to journey "
                        "and before the dispatch table")


class TestNotFound(_TempHarness):
    def test_unknown_task_exits_one_with_single_error_line(self):
        rc, text = self._run_handler("no-such-task")
        self.assertEqual(1, rc)
        lines = text.strip().splitlines()
        self.assertEqual(1, len(lines),
                         f"expected a single error line, got: {text!r}")
        self.assertIn("no-such-task", lines[0])
        self.assertIn(str(self.queue_dir), lines[0])
        self.assertNotIn("# Post-mortem", text)


class TestParkedTaskReport(_TempHarness):
    def _make_parked(self, task_id: str) -> Path:
        task_dir = self.queue_dir / "parked" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": task_id, "status": "parked"}), encoding="utf-8")
        return task_dir

    def _report(self, task_id: str) -> tuple[int, str]:
        return self._run_handler(task_id)

    def test_parked_task_header_and_status(self):
        self._make_parked("task-77")
        rc, text = self._report("task-77")
        self.assertEqual(0, rc)
        lines = text.splitlines()
        self.assertEqual("# Post-mortem: task-77", lines[0])
        self.assertIn("**Status:** parked", text)


if __name__ == "__main__":
    unittest.main()
