"""Slice 2 (001-full-interactions-logged): "no transcript" is never silent.

FR-5 C2/C4 draw the line between the two ways a session ends without a
transcript in the task's `artifacts/sessions/`:

- a *provided* `task_id` that `resolve_task_dir` cannot find (task moved out
  of the queue, bad id, cleaned queue) MUST produce an explicit warning
  naming the stage and the reason — the old code skipped silently;
- a genuinely *absent* task id (`task_id is None`, the autonomous loop) is
  governed by FR-7's pooled recording, NOT by the C2 skip-warning, so it
  must not emit "no transcript written" here.

All three cases run through a real `run_pi_session` subprocess driven by a
fake `pi` script on `PATH` (temp dirs only, never the real binary — `setUp`
asserts `shutil.which` resolves inside the temp dir).

Run from the repo root:  python3 -m unittest tests.test_transcript_skip_warning
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

ASSISTANT_TEXT = "## Summary\nAll good.\n\nVERDICT: done"


def _cfg(work_dir: Path) -> Config:
    return Config(
        work_dir=work_dir,
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
    """Write an executable `pi` that replies with one assistant message_end."""
    body = textwrap.dedent(f"""
        import json
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


class TranscriptSkipWarningTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.queue_dir = self.work_dir / "queue"
        (self.queue_dir / "active").mkdir(parents=True)

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
        kwargs.setdefault("stage", Stage.SLICING)
        return self.runner.run("m", self.work_repo, "Assess the spec.", **kwargs)

    def _skip_warnings(self) -> list[str]:
        return [line for line in self.lines if "no transcript written" in line]

    def test_unresolvable_task_id_logs_warning_naming_stage_and_reason(self):
        result = self._run(task_id="ghost-task")
        warnings = self._skip_warnings()
        self.assertEqual(
            len(warnings), 1,
            f"exactly one C2 skip warning expected; log: {self.lines}")
        self.assertIn("slicing", warnings[0])
        self.assertIn("ghost-task", warnings[0])
        self.assertIn("task dir not found", warnings[0])

    def test_unresolvable_task_id_still_completes_session_and_stats_row(self):
        result = self._run(task_id="ghost-task")
        self.assertTrue(result.ok, f"skip warning must not fail the session: {self.lines}")
        self.assertEqual(len(self.store.for_task("ghost-task")), 1,
                         "the stats row must still be recorded")

    def test_absent_task_id_does_not_emit_c2_skip_warning(self):
        result = self._run(task_id=None)
        self.assertTrue(result.ok, f"log: {self.lines}")
        self.assertEqual(
            self._skip_warnings(), [],
            "task_id=None defers to FR-7 pooled recording, not the C2 warning")

    def test_resolvable_task_id_logs_no_skip_warning(self):
        task_dir = self.queue_dir / "active" / "t1"
        task_dir.mkdir(parents=True)
        result = self._run(task_id="t1")
        self.assertTrue(result.ok, f"log: {self.lines}")
        self.assertEqual(self._skip_warnings(), [])
        transcript = task_dir / "artifacts" / "sessions" / "001-slicing.md"
        self.assertTrue(transcript.is_file(),
                        f"transcript should exist; log: {self.lines}")


if __name__ == "__main__":
    unittest.main()
