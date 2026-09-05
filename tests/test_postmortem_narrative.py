"""Slice 3 — review-summary classification, narrative, UNKNOWN/active.

Completes the spec §5 classification chain: after the telemetry scan finds
no signal, the park/fail reason from `<queue>/review/<task_id>.md`'s
Executive summary classifies the stop (rule 3); with nothing at all the mode
is UNKNOWN (rule 4). The renderer adds `## What happened` (2–5 sentences
naming the reached stage, the attempt count, the stopping session and the
recorded reason) and the `active/` progress-snapshot note (spec §9).

Covered here:
  * `model_rejection` from three kickback rows plus the "loop exceeded"
    park reason, and from the reason alone with signal-free telemetry
    (AC3 remainder);
  * `unknown` for a parked task with no telemetry signals and no review
    summary (AC3 remainder);
  * every §5 rule-3 reason wording: `over context`, `timeout`, `crash`,
    "loop exceeded", "still failing", "not delivered in N …",
    "review after N …", plus the unmatched-reason UNKNOWN fallthrough;
  * the active-task note line, with the mode forced UNKNOWN even when the
    telemetry carries a signal;
  * the narrative quoting the exact recorded reason, its stage/session/
    verdict facts, and its no-sessions form;
  * missing / unreadable / section-less review summaries tolerated.
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
from harness.core.enums import TaskStatus  # noqa: E402
from harness.core.postmortem import (  # noqa: E402
    ACTIVE_TASK_NOTE,
    PostMortemFailureMode,
    classify_failure,
    classify_from_reason,
    read_review_summary,
)


def _row(**overrides) -> dict:
    """One synthetic `sessions.jsonl` row with sane defaults."""
    row = {
        "ts": "2026-02-01T10:00:00",
        "task_id": "task-pm3",
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
    """A temp work dir wired through HARNESS_CONFIG, like the earlier slices."""

    TASK_ID = "task-pm3"

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

    def _make_task(self, status: str = "parked",
                   state: dict | None = None) -> Path:
        task_dir = self.queue_dir / status / self.TASK_ID
        task_dir.mkdir(parents=True)
        payload = {"id": self.TASK_ID, "status": status}
        payload.update(state or {})
        (task_dir / "task.json").write_text(json.dumps(payload),
                                            encoding="utf-8")
        return task_dir

    def _write_rows(self, *rows: dict) -> None:
        self.stats_path.parent.mkdir(parents=True, exist_ok=True)
        with self.stats_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def _write_review(self, reason: str) -> Path:
        review_dir = self.queue_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        path = review_dir / f"{self.TASK_ID}.md"
        path.write_text(
            f"# Task: {self.TASK_ID}\n\n"
            "**Status:** parked\n**Date:** 2026-02-01T11:00:00\n\n"
            "## Original requirement\n\nbuild the thing\n\n"
            f"## Executive summary\n\n{reason}\n\n"
            "## Artifacts\n\n- spec: `spec.md`\n",
            encoding="utf-8")
        return path

    def _report(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers.cmd_post_mortem(self.TASK_ID, save=False)
        return rc, out.getvalue()


class TestReasonClassification(_TempHarness):
    def test_kickback_loop_with_reason_classifies_rejection(self):
        self._make_task()
        self._write_rows(
            _row(verdict="pass", outcome="pass"),
            _row(verdict="kickback", outcome="kickback"),
            _row(verdict="kickback", outcome="kickback"),
            _row(verdict="kickback", outcome="kickback"),
        )
        self._write_review("slice fit check loop exceeded")
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Model rejection loop "
                      "(`model_rejection`)", text)
        self.assertIn('Reason recorded at park: "slice fit check loop '
                      'exceeded"', text)

    def test_reason_alone_classifies_without_telemetry_signals(self):
        self._make_task()
        self._write_rows(_row(), _row())
        self._write_review("slice 3 not delivered in 3 implementation "
                           "iterations")
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Model rejection loop "
                      "(`model_rejection`)", text)
        self.assertNotIn("## Point of failure", text)

    def test_parked_without_signals_is_unknown(self):
        self._make_task()
        self._write_rows(_row(), _row())
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Failure mode:** Unknown (`unknown`)", text)
        self.assertNotIn("## Point of failure", text)
        self.assertNotIn("Reason recorded at park", text)


class TestReasonRulesUnit(unittest.TestCase):
    """Every §5 rule-3 wording, at the data edge."""

    def test_over_context_budget_reason(self):
        self.assertEqual(
            PostMortemFailureMode.CONTEXT_BUDGET,
            classify_from_reason("over context budget: peak=200000 "
                                 "limit=131072"))

    def test_over_cap_shorthand_reason(self):
        self.assertEqual(PostMortemFailureMode.CONTEXT_BUDGET,
                         classify_from_reason("session went over-cap"))

    def test_timeout_reason(self):
        self.assertEqual(PostMortemFailureMode.WALL_CLOCK_TIMEOUT,
                         classify_from_reason("session timed out at the cap"))
        self.assertEqual(PostMortemFailureMode.WALL_CLOCK_TIMEOUT,
                         classify_from_reason("wall clock timeout reached"))

    def test_crash_reason(self):
        self.assertEqual(PostMortemFailureMode.CRASH,
                         classify_from_reason("pi subprocess crashed (-9)"))

    def test_rejection_wordings(self):
        for reason in ("spec kickback loop exceeded (3)",
                       "feasibility still failing after spec revision",
                       "slice 2 not delivered in 4 implementation iterations",
                       "slice 2 failed spec review after 2 iterations"):
            with self.subTest(reason=reason):
                self.assertEqual(PostMortemFailureMode.MODEL_REJECTION,
                                 classify_from_reason(reason))

    def test_unmatched_reason_returns_none(self):
        self.assertIsNone(classify_from_reason("operator parked by hand"))
        self.assertIsNone(classify_from_reason(""))

    def test_no_signals_and_no_reason_falls_through_unknown(self):
        failure = classify_failure(status=TaskStatus.PARKED,
                                   rows=[_row()],
                                   park_reason="operator parked by hand")
        self.assertEqual(PostMortemFailureMode.UNKNOWN, failure.mode)
        self.assertIsNone(failure.session_index)

    def test_telemetry_signal_beats_the_park_reason(self):
        failure = classify_failure(
            status=TaskStatus.PARKED,
            rows=[_row(notes=" [crashed: child exited with -9]",
                       verdict="unknown", outcome="unknown", rc=-9)],
            park_reason="slice fit check loop exceeded")
        self.assertEqual(PostMortemFailureMode.CRASH, failure.mode)
        self.assertEqual(1, failure.session_index)


class TestActiveTask(_TempHarness):
    def test_active_task_gets_progress_note_and_unknown_mode(self):
        self._make_task(status="active")
        self._write_rows(_row(), _row())
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Status:** active", text)
        self.assertIn(ACTIVE_TASK_NOTE, text)
        self.assertIn("**Failure mode:** Unknown (`unknown`)", text)

    def test_active_task_note_precedes_what_happened(self):
        self._make_task(status="active")
        self._write_rows(_row())
        _, text = self._report()
        self.assertLess(text.index(ACTIVE_TASK_NOTE),
                        text.index("## What happened"))

    def test_active_task_mode_stays_unknown_with_a_signal(self):
        self._make_task(status="active")
        self._write_rows(_row(notes=" [crashed: oom]", verdict="unknown",
                              outcome="unknown", rc=-9))
        _, text = self._report()
        self.assertIn("**Failure mode:** Unknown (`unknown`)", text)
        self.assertNotIn("## Point of failure", text)


class TestNarrative(_TempHarness):
    def test_narrative_quotes_the_exact_reason(self):
        reason = "slice 3 not delivered in 3 implementation iterations"
        self._make_task(state={"checkpointed_stages": ["spec", "feasibility"],
                               "checkpointed_slices": ["1", "2"]})
        self._write_rows(
            _row(),
            _row(stage="slice_review", verdict="kickback",
                 outcome="kickback"),
            _row(stage="slice_review", verdict="kickback",
                 outcome="kickback", ts="2026-02-03T09:15:00"),
        )
        self._write_review(reason)
        _, text = self._report()
        section = text.split("## What happened", 1)[1]
        self.assertIn("The task reached stage `feasibility` with 2 slice(s) "
                      "completed (1, 2).", section)
        self.assertIn("It attempted 3 session(s), the last one at stage "
                      "`slice_review`.", section)
        self.assertIn("The task stopped at session #3 on 2026-02-03 with "
                      "verdict `kickback` and outcome `kickback`.", section)
        self.assertIn(f'Reason recorded at park: "{reason}"', section)

    def test_narrative_counts_iterations_at_the_final_stage(self):
        self._make_task()
        self._write_rows(
            _row(),
            _row(stage="slice_review", iteration=1, verdict="kickback",
                 outcome="kickback"),
            _row(stage="slice_review", iteration=2, verdict="kickback",
                 outcome="kickback"),
            _row(stage="slice_review", iteration=3, verdict="kickback",
                 outcome="kickback"),
        )
        _, text = self._report()
        section = text.split("## What happened", 1)[1]
        self.assertIn("It attempted 4 session(s), the last one at stage "
                      "`slice_review` over 3 iteration(s).", section)

    def test_narrative_omits_iteration_clause_at_one_iteration(self):
        self._make_task()
        self._write_rows(_row(), _row(iteration="bogus"))
        _, text = self._report()
        section = text.split("## What happened", 1)[1]
        self.assertIn("It attempted 2 session(s), the last one at stage "
                      "`slice_implement`.", section)
        self.assertNotIn("iteration(s)", section)

    def test_narrative_without_review_summary_omits_the_reason(self):
        self._make_task()
        self._write_rows(_row())
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("## What happened", text)
        self.assertNotIn("Reason recorded at park", text)

    def test_narrative_with_no_sessions_states_it(self):
        self._make_task()
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("No sessions were recorded for this task", text)

    def test_narrative_omits_reached_clause_without_checkpoints(self):
        self._make_task()
        self._write_rows(_row())
        _, text = self._report()
        section = text.split("## What happened", 1)[1]
        self.assertNotIn("The task reached stage", section)


class TestReviewSummaryReader(_TempHarness):
    def test_reads_the_executive_summary_section(self):
        self._write_review("review after 2 iterations parked the task")
        summary = read_review_summary(self.queue_dir, self.TASK_ID)
        self.assertIsNotNone(summary)
        self.assertEqual("review after 2 iterations parked the task",
                         summary.reason)

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_review_summary(self.queue_dir, self.TASK_ID))

    def test_file_without_the_section_returns_empty_reason(self):
        review_dir = self.queue_dir / "review"
        review_dir.mkdir(parents=True)
        (review_dir / f"{self.TASK_ID}.md").write_text(
            "# Task: task-pm3\n\n## Artifacts\n\n- none\n", encoding="utf-8")
        summary = read_review_summary(self.queue_dir, self.TASK_ID)
        self.assertIsNotNone(summary)
        self.assertEqual("", summary.reason)

    def test_unreadable_file_returns_none(self):
        review_dir = self.queue_dir / "review"
        review_dir.mkdir(parents=True)
        path = review_dir / f"{self.TASK_ID}.md"
        path.write_text("## Executive summary\n\nboom\n", encoding="utf-8")
        with mock.patch.object(Path, "read_text",
                               side_effect=OSError("permission denied")):
            self.assertIsNone(
                read_review_summary(self.queue_dir, self.TASK_ID))


if __name__ == "__main__":
    unittest.main()
