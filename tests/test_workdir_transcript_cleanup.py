"""Slice 3 (001-full-interactions-logged): clean workdir, full transcript names.

Companion to `test_workdir_persistence.py` (which pins workdir *resolution*).
These tests pin what the session layer leaves behind and how transcripts are
named, end-to-end through a real `run_pi_session` subprocess driven by a fake
`pi` script on `PATH` (never the real binary — `setUp` asserts `shutil.which`
resolves inside the temp dir):

- with a `task_id`, no hidden `.pi-session-*` capture (`.out` or `.err`)
  survives in the implementation workdir — the transcript holds the contents;
- with no `task_id` (FR-7 pool), the capture is removed once the pooled
  transcript is durably written (FR-6 covers both pools);
- a session that gets no transcript at all (unresolvable `task_id`) keeps the
  legacy workdir `.out`/`.err` placement — content is never lost twice;
- filenames carry `[-slice-<id>][-iter-<n>]`, and a retried stage
  (iteration 2, same stage/slice) gets its own file — nothing is overwritten;
- a resumed task whose `artifacts/sessions/` was restored from a backup keeps
  numbering past the restored files (AC 9);
- pipeline artifact filing falls back to the in-memory output once the
  transient capture is gone (kickback reports, progress notes, feedback).

Run from the repo root:  python3 -m unittest tests.test_workdir_transcript_cleanup
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
from harness.core.enums import Stage, Verdict
from harness.core.session import SessionResult, SessionRunner
from harness.core.stats import StatsStore
from harness.workflow.pipeline import file_session_output

ASSISTANT_TEXT = "All good.\n\nVERDICT: done"
STDERR_TEXT = "fake pi: deprecated flag\n"
PEAK_TOKENS = 42


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
    """Write an executable `pi` that replies on stdout and warns on stderr.

    The stderr line makes `run_pi_session` write the `<out_file>.err` capture,
    so the cleanup assertions cover both transient files, and the transcript
    gets a real `## Stderr` section.
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
                "usage": {{"totalTokens": {PEAK_TOKENS}}},
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


class WorkdirTranscriptCleanupTest(unittest.TestCase):
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

    def _run(self, prompt: str = "Implement the thing.", **kwargs):
        kwargs.setdefault("task_id", self.task_id)
        kwargs.setdefault("stage", Stage.SPEC_AUTHOR)
        return self.runner.run("m", self.work_repo, prompt, **kwargs)

    # ------------------------------------------------------------------
    # (a) task_id set: zero hidden session files left in the workdir
    # ------------------------------------------------------------------
    def test_task_id_session_leaves_no_capture_in_workdir(self):
        result = self._run()
        self.assertTrue(result.ok, f"session should succeed: {self.lines}")
        leftovers = sorted(p.name for p in self.work_repo.glob(".pi-session-*"))
        self.assertEqual(leftovers, [],
                         f"transient capture survived in the workdir")
        transcript = self.sessions_dir / "001-spec_author.md"
        self.assertTrue(transcript.is_file())
        # The capture's contents are not lost with the file: output and
        # stderr are both in the transcript.
        text = transcript.read_text()
        self.assertIn(ASSISTANT_TEXT, text)
        self.assertIn("## Stderr", text)
        self.assertIn(STDERR_TEXT.strip(), text)

    # ------------------------------------------------------------------
    # (b) no task_id: pooled transcript written, capture removed (FR-6)
    # ------------------------------------------------------------------
    def test_pooled_session_removes_capture_after_pooled_write(self):
        result = self._run("direct use", task_id=None)
        self.assertTrue(result.ok)
        self.assertFalse(self.sessions_dir.exists(),
                         "task-less sessions must not touch the task pool")
        pool = sorted((self.work_dir / "artifacts" / "sessions").glob("*.md"))
        self.assertEqual(len(pool), 1, f"pooled transcript missing: {self.lines}")
        self.assertIn(ASSISTANT_TEXT, pool[0].read_text())
        # FR-6: the pooled transcript is durable, so the capture goes too.
        leftovers = sorted(p.name for p in self.work_repo.glob(".pi-session-*"))
        self.assertEqual(leftovers, [],
                         "capture survived a successful pooled transcript")

    # ------------------------------------------------------------------
    # (b2) no transcript at all: legacy capture untouched
    # ------------------------------------------------------------------
    def test_unresolvable_task_id_keeps_legacy_capture(self):
        result = self._run("orphan", task_id="ghost-task")
        self.assertTrue(result.ok)
        warnings = [line for line in self.lines
                    if "no transcript written for stage" in line]
        self.assertEqual(len(warnings), 1, f"C2 warning expected: {self.lines}")
        legacy = list(self.work_repo.glob(".pi-session-*.out"))
        self.assertEqual(len(legacy), 1, f"legacy .out missing: {self.lines}")
        self.assertEqual(legacy[0].read_text(), ASSISTANT_TEXT)
        # The `.err` sibling is part of the legacy placement too.
        self.assertTrue(Path(str(legacy[0]) + ".err").is_file())

    # ------------------------------------------------------------------
    # (c) full naming scheme; a retry never overwrites
    # ------------------------------------------------------------------
    def test_retried_slice_iteration_gets_distinct_transcripts(self):
        self._run("attempt one", stage=Stage.SLICE_IMPLEMENT,
                  slice_id="3", iteration=1)
        self._run("attempt two", stage=Stage.SLICE_IMPLEMENT,
                  slice_id="3", iteration=2)
        names = sorted(p.name for p in self.sessions_dir.iterdir())
        self.assertEqual(names, ["001-slice_implement-slice-3.md",
                                 "002-slice_implement-slice-3-iter-2.md"])
        first = (self.sessions_dir / names[0]).read_text()
        second = (self.sessions_dir / names[1]).read_text()
        # Neither file was overwritten by the other attempt.
        self.assertIn("attempt one", first)
        self.assertNotIn("attempt two", first)
        self.assertIn("attempt two", second)
        self.assertIn("- slice: 3", second)
        self.assertIn("- iteration: 2", second)

    # ------------------------------------------------------------------
    # (d) resume: numbering continues past restored transcripts (AC 9)
    # ------------------------------------------------------------------
    def test_restored_transcripts_advance_numbering(self):
        # Simulate the state after resume restored `artifacts/` from its
        # backup: transcripts on disk, stats store fresh (the JSONL lives
        # outside the task dir and was not restored).
        self.sessions_dir.mkdir(parents=True)
        restored = []
        for n in (1, 2, 3):
            path = self.sessions_dir / f"{n:03d}-spec_author.md"
            path.write_text(f"restored transcript {n}\n")
            restored.append(path)

        self._run("post-resume session")

        new = self.sessions_dir / "004-spec_author.md"
        self.assertTrue(new.is_file(),
                        f"expected 004, got {sorted(p.name for p in self.sessions_dir.iterdir())}")
        for path in restored:
            self.assertTrue(path.is_file(), f"restored transcript {path.name} vanished")
            self.assertIn("restored transcript", path.read_text())

    # ------------------------------------------------------------------
    # pipeline artifact filing survives the missing capture
    # ------------------------------------------------------------------
    def test_file_session_output_copies_capture_when_present(self):
        src = self.work_repo / "capture.out"
        src.write_text("captured bytes")
        dest = self.task_dir / "artifacts" / "kickback_a_1.md"
        file_session_output(self._result(output="memory text", out_file=src), dest)
        self.assertEqual(dest.read_text(), "captured bytes")

    def test_file_session_output_falls_back_to_memory(self):
        dest = self.task_dir / "artifacts" / "progress" / "slice-1.md"
        file_session_output(
            self._result(output="memory text",
                         out_file=self.work_repo / "gone.out"), dest)
        self.assertEqual(dest.read_text(), "memory text")

    @staticmethod
    def _result(output: str, out_file: Path) -> SessionResult:
        return SessionResult(ok=True, verdict=Verdict.DONE, peak_tokens=1,
                             duration_s=0.0, output=output, out_file=out_file,
                             crashed=False)


if __name__ == "__main__":
    unittest.main()
