"""Slice 3 (001-full-interactions-logged): FR-7 pooled transcripts.

Autonomous-loop sessions run with `task_id=None`: no task dir, no journey to
link from. They MUST still be recorded — as full FR-1 transcripts under
`<work_dir>/artifacts/sessions/`, named with a sortable, collision-free
ISO-8601 UTC timestamp prefix (two sessions in the same wall-clock second do
not collide), with a log line naming the pool path. A pooled write that fails
falls back to the FR-5 C4 skip-warning (warn-and-continue; the pipeline is
unaffected).

Integration cases run a real `run_pi_session` subprocess driven by a fake
`pi` script on `PATH` (temp dirs only — `setUp` asserts `shutil.which`
resolves inside the temp dir).

Run from the repo root:  python3 -m unittest tests.test_pooled_transcripts
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.core.transcripts import (
    TranscriptRecord,
    pooled_sessions_dir,
    pooled_timestamp,
    render_transcript,
    write_pooled_transcript,
)

ASSISTANT_TEXT = "## Idea\nA new feature.\n\nVERDICT: done"

# YYYYMMDDTHHMMSS.ffffffZ — sortable ISO-8601 basic form, sub-second, UTC.
_POOLED_NAME = re.compile(
    r"^\d{8}T\d{6}\.\d{6}Z-(?P<stage>[a-z_]+)\.md$")


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
    """Write an executable `pi` that replies with one assistant message_end."""
    body = textwrap.dedent(f"""
        import json, sys
        if "--list-models" in sys.argv:
            print("m")
            sys.exit(0)
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


def _pooled_record(stderr: str = "") -> TranscriptRecord:
    """A task-less record: no sequence, no task id (FR-7 identity)."""
    return TranscriptRecord(
        sequence=None, task_id=None, stage="autonomous_suggest",
        timestamp="2026-01-01T00:00:00+0000", model="m", duration_s=1.0,
        peak_tokens=10, rc=0, verdict="done", crashed=False,
        prompt="p", output="o", stderr=stderr,
    )


class PooledTranscriptIntegrationTest(unittest.TestCase):
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
        kwargs.setdefault("stage", Stage.AUTONOMOUS_SUGGEST)
        kwargs.setdefault("task_id", None)
        return self.runner.run("m", self.work_repo, "Suggest a task.",
                               **kwargs)

    def _pool_files(self) -> list[Path]:
        pool = pooled_sessions_dir(self.work_dir)
        return sorted(pool.glob("*.md")) if pool.is_dir() else []

    def test_task_less_session_writes_pooled_transcript(self):
        result = self._run()
        self.assertTrue(result.ok, f"log: {self.lines}")
        files = self._pool_files()
        self.assertEqual(len(files), 1, f"one pooled transcript expected; log: {self.lines}")
        self.assertRegex(files[0].name, _POOLED_NAME)

    def test_log_line_names_the_pool_path(self):
        result = self._run()
        self.assertTrue(result.ok, f"log: {self.lines}")
        [written] = self._pool_files()
        notes = [line for line in self.lines if "pooled transcript" in line]
        self.assertEqual(len(notes), 1, f"log: {self.lines}")
        self.assertIn(str(written), notes[0])

    def test_two_sessions_in_one_second_do_not_collide(self):
        self.assertTrue(self._run().ok)
        self.assertTrue(self._run().ok)
        files = self._pool_files()
        self.assertEqual(len(files), 2,
                         f"two distinct pooled files expected; got {files}")
        self.assertEqual(len({f.name for f in files}), 2)

    def test_pooled_transcript_has_fr1_sections(self):
        self.assertTrue(self._run().ok)
        text = self._pool_files()[0].read_text(encoding="utf-8")
        self.assertIn("# Session autonomous_suggest (pooled)", text)
        for line in ("- stage: autonomous_suggest", "- iteration: 1",
                     "- model: m", "- duration_s:", "- peak_tokens: 42",
                     "- rc: 0", "- verdict: done", "- crashed: false"):
            self.assertIn(line, text)
        self.assertIn("## Prompt", text)
        self.assertIn("CONTEXT BUDGET", text)  # budget preamble included
        self.assertIn("## Output", text)
        self.assertIn(ASSISTANT_TEXT, text)
        self.assertNotIn("## Stderr", text)  # rc=0, empty stderr: no section

    def test_pooled_session_emits_no_skip_warning(self):
        self.assertTrue(self._run().ok)
        self.assertEqual(
            [line for line in self.lines if "transcript written for stage" in line],
            [], "successful pooled recording must not warn")

    def test_task_scoped_session_does_not_touch_the_pool(self):
        task_dir = self.queue_dir / "active" / "t1"
        task_dir.mkdir(parents=True)
        result = self._run(task_id="t1", stage=Stage.SLICING)
        self.assertTrue(result.ok, f"log: {self.lines}")
        self.assertEqual(self._pool_files(), [],
                         "pool is only for task-less sessions")

    def test_failed_pooled_write_warns_and_continues(self):
        artifacts = self.work_dir / "artifacts"
        artifacts.mkdir()
        artifacts.chmod(0o500)  # read-only: sessions/ cannot be created
        self.addCleanup(artifacts.chmod, 0o755)
        result = self._run()
        artifacts.chmod(0o755)  # restore before assertions/cleanup on failure
        warnings = [line for line in self.lines
                    if "no pooled transcript written for stage" in line]
        self.assertEqual(len(warnings), 1,
                         f"FR-5 C4 fallback warning expected; log: {self.lines}")
        self.assertIn("autonomous_suggest", warnings[0])
        self.assertTrue(result.ok, "a failed audit write must not fail the session")
        self.assertEqual(len(self.store.all()), 1,
                         "the stats row must still be recorded")


class PooledTranscriptUnitTest(unittest.TestCase):
    def test_timestamp_is_sortable_and_subsecond(self):
        stamps = [pooled_timestamp() for _ in range(50)]
        self.assertTrue(all(re.match(r"^\d{8}T\d{6}\.\d{6}Z$", s)
                            for s in stamps))
        self.assertEqual(len(set(stamps)), len(stamps),
                         "sub-second resolution must not repeat within a run")

    def test_same_timestamp_falls_back_to_unique_names(self):
        """A pre-existing name (clock adjustment) never overwrites a pooled file."""
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            fixed = "20260101T000000.000000Z"
            with mock.patch("harness.core.transcripts.pooled_timestamp",
                            return_value=fixed):
                first = write_pooled_transcript(work_dir, _pooled_record(),
                                                lambda msg: None)
                second = write_pooled_transcript(work_dir, _pooled_record(),
                                                 lambda msg: None)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, f"{fixed}-autonomous_suggest.md")
            self.assertEqual(second.name, f"{fixed}-autonomous_suggest-2.md")

    def test_failed_pooled_write_warns_naming_stage_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            artifacts = work_dir / "artifacts"
            artifacts.mkdir()
            artifacts.chmod(0o500)
            lines: list[str] = []
            try:
                path = write_pooled_transcript(work_dir, _pooled_record(),
                                               lines.append)
            finally:
                artifacts.chmod(0o755)
            self.assertIsNone(path)
            self.assertEqual(len(lines), 1)
            self.assertIn("no pooled transcript written for stage", lines[0])
            self.assertIn("autonomous_suggest", lines[0])

    def test_render_pooled_record_keeps_fr1_layout(self):
        text = render_transcript(_pooled_record(stderr="boom"))
        self.assertIn("# Session autonomous_suggest (pooled)", text)
        self.assertNotIn("(None)", text)
        self.assertNotIn("Session None", text)
        self.assertIn("## Prompt", text)
        self.assertIn("## Output", text)
        self.assertIn("## Stderr", text)  # non-empty stderr always gets a section
        self.assertIn("boom", text)


if __name__ == "__main__":
    unittest.main()
