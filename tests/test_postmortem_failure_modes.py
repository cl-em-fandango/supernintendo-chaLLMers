"""Slice 2 — failure-mode taxonomy and the Point-of-failure section.

The analyzer classifies a stopped task from its `sessions.jsonl` rows plus
the matched transcripts (spec §5 rules 1–2) and the renderer prints
`**Failure mode:** <label> (`<wire value>`)` and, when a session
classified the stop, the `## Point of failure` section naming that session,
its raw evidence string (truncated to 200 chars) and its transcript path
(spec §7).

Covered here:
  * one fixture per telemetry-detectable mode: `context_budget` (AC2),
    `wall_clock_timeout`, `crash`, `error_other`, `completed` (AC3);
  * the within-session priority tests (AC4): `[crashed:` + `kickback`
    classifies as `crash`; `over-cap` + `timeout` text classifies as
    `wall_clock_timeout`;
  * the recency rule: an older timeout row vs a newer kickback row
    classifies as `model_rejection` and names the newer session;
  * transcript-only signals (`crashed: true`, `over context cap` in the
    `- error:` line) and `no transcript on disk` for unpaired rows;
  * the 200-char evidence truncation with `…`.
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
from harness.core.postmortem import (  # noqa: E402
    PostMortemFailureMode,
    TranscriptDiagnostics,
    detect_session_signals,
    read_transcript_diagnostics,
)


def _row(**overrides) -> dict:
    """One synthetic `sessions.jsonl` row with sane defaults."""
    row = {
        "ts": "2026-02-01T10:00:00",
        "task_id": "task-pm",
        "stage": "slice_implement",
        "slice": "3",
        "iteration": 1,
        "model": "test-model",
        "prompt_chars": 100,
        "duration_s": 12.5,
        "peak_tokens": 40000,
        "verdict": "pass",
        "outcome": "pass",
        "rc": 0,
        "session_file": None,
        "notes": "",
    }
    row.update(overrides)
    return row


class _TempHarness(unittest.TestCase):
    """A temp work dir wired through HARNESS_CONFIG, like the spine tests."""

    TASK_ID = "task-pm"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name) / "work"
        self.queue_dir = self.work / "queue"
        self.stats_path = self.work / "stats" / "sessions.jsonl"
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps(
            {"harnessExecutionAndQueueDir": str(self.work)}), encoding="utf-8")
        self._env = mock.patch.dict(os.environ,
                                    {"HARNESS_CONFIG": str(cfg_path)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _make_task(self, status: str = "parked") -> Path:
        task_dir = self.queue_dir / status / self.TASK_ID
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": self.TASK_ID, "status": status}), encoding="utf-8")
        return task_dir

    def _write_rows(self, *rows: dict) -> None:
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        with self.stats_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def _make_transcript(self, name: str, *, crashed: bool = False,
                         error: str = "") -> Path:
        sessions_dir = (self.queue_dir / "parked" / self.TASK_ID
                        / "artifacts" / "sessions")
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / name
        lines = [
            "# Session 001: slice_implement",
            "",
            "- task: task-pm",
            "- model: test-model",
            "- duration: 12.5s",
            "- rc: 1",
            "- verdict: kickback",
            f"- crashed: {'true' if crashed else 'false'}",
        ]
        if error:
            lines.append(f"- error: {error}")
        lines += ["", "## Prompt", "", "prompt text", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _report(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers.cmd_post_mortem(self.TASK_ID, save=False)
        return rc, out.getvalue()


class TestTelemetryModes(_TempHarness):
    def test_context_budget_from_over_cap_notes(self):
        self._make_task()
        self._write_rows(
            _row(),
            _row(verdict="fail", outcome="fail", rc=0,
                 notes=" over-cap peak=171234 limit=131072",
                 peak_tokens=171234),
        )
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Context budget exhaustion "
                      "(`context_budget`)", text)
        self.assertIn("## Point of failure", text)
        self.assertIn("Session #2 — slice_implement slice 3, model test-model",
                      text)
        self.assertIn("over-cap peak=171234 limit=131072", text)
        self.assertIn("no transcript on disk", text)

    def test_wall_clock_timeout_from_notes(self):
        self._make_task()
        self._write_rows(_row(notes="session timed out after cap",
                              verdict="unknown", outcome="unknown"))
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Wall-clock timeout "
                      "(`wall_clock_timeout`)", text)
        self.assertIn("session timed out after cap", text)

    def test_crash_from_notes(self):
        self._make_task()
        self._write_rows(_row(notes=" [crashed: child exited with -9]",
                              verdict="unknown", outcome="unknown", rc=-9))
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Subprocess crash (`crash`)", text)
        self.assertIn("[crashed: child exited with -9]", text)

    def test_error_other_from_rc(self):
        self._make_task()
        self._write_rows(_row(verdict="unknown", outcome="unknown", rc=2))
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Error, unclassified signal "
                      "(`error_other`)", text)
        self.assertIn("rc=2", text)

    def test_completed_done_task_has_no_point_of_failure(self):
        self._make_task(status="done")
        self._write_rows(_row(), _row())
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Completed (`completed`)", text)
        self.assertNotIn("## Point of failure", text)


class TestPriorityAndRecency(_TempHarness):
    def test_crash_beats_rejection_in_one_session(self):
        self._make_task()
        self._write_rows(_row(notes=" [crashed: oom]",
                              verdict="kickback", outcome="kickback"))
        _, text = self._report()
        self.assertIn("**Failure mode:** Subprocess crash (`crash`)", text)

    def test_timeout_beats_context_budget_in_one_session(self):
        self._make_task()
        self._write_rows(_row(
            notes=" over-cap peak=200000 limit=131072; timed out waiting",
            verdict="fail", outcome="fail"))
        _, text = self._report()
        self.assertIn("**Failure mode:** Wall-clock timeout "
                      "(`wall_clock_timeout`)", text)

    def test_recency_newer_rejection_beats_older_timeout(self):
        self._make_task()
        self._write_rows(
            _row(notes="run timed out", verdict="unknown",
                 outcome="unknown"),
            _row(verdict="kickback", outcome="kickback"),
        )
        _, text = self._report()
        self.assertIn("**Failure mode:** Model rejection loop "
                      "(`model_rejection`)", text)
        self.assertIn("Session #2", text)
        self.assertIn("verdict=kickback outcome=kickback", text)


class TestTranscriptSignals(_TempHarness):
    def test_crashed_true_in_transcript_metadata(self):
        self._make_task()
        self._write_rows(_row(verdict="unknown", outcome="unknown", rc=1))
        self._make_transcript("001-slice_implement-slice-3.md", crashed=True)
        _, text = self._report()
        self.assertIn("**Failure mode:** Subprocess crash (`crash`)", text)
        self.assertIn("crashed: true", text)
        self.assertIn("001-slice_implement-slice-3.md", text)

    def test_over_context_cap_in_transcript_error_line(self):
        self._make_task()
        self._write_rows(_row(verdict="fail", outcome="fail"))
        self._make_transcript(
            "001-slice_implement-slice-3.md",
            error="over context cap: peak=200000 tokens limit=131072")
        _, text = self._report()
        self.assertIn("**Failure mode:** Context budget exhaustion "
                      "(`context_budget`)", text)
        self.assertIn("over context cap: peak=200000 tokens limit=131072",
                      text)

    def test_read_transcript_diagnostics_parses_header_only(self):
        self._make_task()
        path = self._make_transcript(
            "001-slice_implement-slice-3.md",
            crashed=True, error="boom - error: decoy line below sections")
        diagnostics = read_transcript_diagnostics(path)
        self.assertIsInstance(diagnostics, TranscriptDiagnostics)
        self.assertEqual("boom - error: decoy line below sections",
                         diagnostics.error)
        self.assertTrue(diagnostics.crashed)

    def test_read_transcript_diagnostics_absent_path(self):
        self.assertIsNone(read_transcript_diagnostics(None))


class TestEvidenceTruncation(_TempHarness):
    def test_long_evidence_truncated_to_200_chars(self):
        self._make_task()
        notes = " [crashed: " + "x" * 300 + "]"
        self._write_rows(_row(notes=notes, verdict="unknown",
                              outcome="unknown", rc=-9))
        _, text = self._report()
        evidence_line = next(
            line for line in text.splitlines()
            if line.startswith("- Evidence: `"))
        quoted = evidence_line[len("- Evidence: `"):-1]
        self.assertEqual(201, len(quoted))
        self.assertTrue(quoted.endswith("…"))
        self.assertNotIn("xxx" * 100, text)


class TestDetectSessionSignalsUnit(unittest.TestCase):
    """Direct coverage of the §5 detection rules at the data edge."""

    def test_no_signals_on_a_passing_row(self):
        signals = detect_session_signals(_row())
        self.assertTrue(signals.is_empty())

    def test_over_cap_matches_notes_not_transcript_rule(self):
        signals = detect_session_signals(
            _row(notes=" over-cap peak=9 limit=8"),
            TranscriptDiagnostics(error="unrelated failure"))
        self.assertEqual("over-cap peak=9 limit=8",
                         signals.context_budget.strip()[:24])

    def test_over_cap_prefix_needs_the_space_variant_in_transcript(self):
        signals = detect_session_signals(
            _row(), TranscriptDiagnostics(error="over context cap: peak=9"))
        self.assertTrue(signals.context_budget)

    def test_rejection_sets(self):
        signals = detect_session_signals(_row(verdict="kickout",
                                              outcome="kickout"))
        self.assertEqual("verdict=kickout outcome=kickout", signals.rejection)

    def test_error_other_on_error_outcome(self):
        signals = detect_session_signals(_row(verdict="error",
                                              outcome="error"))
        self.assertEqual("rc=0 outcome=error", signals.error_other)


if __name__ == "__main__":
    unittest.main()
