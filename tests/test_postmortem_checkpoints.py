"""Slice 4 — last successful checkpoint, cross-check, resume, Accomplished.

The report header gains `**Last successful checkpoint:**`, the state-file
checkpoints (ordered per CHECKPOINT_ORDER) are cross-checked against the
latest passing telemetry row, a disagreement is flagged with the
authoritative-state-file note, and a resume-readiness line names the stage
(and slice) a `harness.py resume` would restart from. `## State of play` /
`### Accomplished` lists one bullet per completed stage and slice.

Covered here:
  * AC5: `checkpointed_stages=[spec, feasibility]` plus
    `checkpointed_slices=["1", "2"]` renders both slices as accomplished and
    the resume line names stage `slices`, slice `3`;
  * the disagreement case: a latest passing row the state file does not
    record renders both checkpoints and the authoritative-state note, while
    a covered passing row renders neither;
  * both sources empty: the "No stage checkpointed" text in the header and
    the Accomplished list, with resume naming `spec`;
  * corrupt `task.json`: telemetry-only fallback plus the
    `task.json unreadable` note, status taken from the directory;
  * unit edges: stage ordering, next-slice numbering, the all-checkpointed
    resume line, and the section order of the rendered report.
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
from harness.core.enums import CheckpointStage  # noqa: E402
from harness.core.postmortem import (  # noqa: E402
    DISAGREEMENT_NOTE,
    NO_CHECKPOINT_TEXT,
    UNREADABLE_STATE_NOTE,
    build_checkpoint_report,
)


def _row(**overrides) -> dict:
    """One synthetic `sessions.jsonl` row with sane defaults."""
    row = {
        "ts": "2026-02-01T10:00:00",
        "task_id": "task-pm4",
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

    TASK_ID = "task-pm4"

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

    def _report(self) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = handlers.cmd_post_mortem(self.TASK_ID, save=False)
        return rc, out.getvalue()


class TestAccomplishedCheckpoint(  # noqa: D401 — AC5 vertical case
        _TempHarness):
    def test_ac5_checkpoints_slices_and_resume(self):
        self._make_task(state={"checkpointed_stages": ["spec", "feasibility"],
                               "checkpointed_slices": ["1", "2"]})
        self._write_rows(
            _row(stage="slice_implement", slice="1"),
            _row(stage="slice_implement", slice="2"),
            _row(stage="slice_review", slice="3", verdict="kickback",
                 outcome="kickback"),
        )
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Last successful checkpoint:** feasibility "
                      "/ slices 1, 2", text)
        accomplished = text.split("### Accomplished", 1)[1]
        self.assertIn("- Stage `spec` checkpointed", accomplished)
        self.assertIn("- Stage `feasibility` checkpointed", accomplished)
        self.assertIn("- Slice `1` completed", accomplished)
        self.assertIn("- Slice `2` completed", accomplished)
        self.assertIn(f"A `harness.py resume {self.TASK_ID}` would restart "
                      "at stage `slices`, slice `3`.", text)
        self.assertNotIn(DISAGREEMENT_NOTE, text)

    def test_accomplished_lists_stages_before_slices(self):
        self._make_task(state={"checkpointed_stages": ["spec"],
                               "checkpointed_slices": ["1"]})
        self._write_rows(_row(slice="1"))
        _, text = self._report()
        accomplished = text.split("### Accomplished", 1)[1]
        self.assertLess(accomplished.index("Stage `spec`"),
                        accomplished.index("Slice `1`"))

    def test_section_order_state_of_play(self):
        self._make_task(state={"checkpointed_stages": ["spec"]})
        self._write_rows(_row(verdict="kickback", outcome="kickback"))
        _, text = self._report()
        self.assertLess(text.index("## What happened"),
                        text.index("## State of play"))
        self.assertLess(text.index("## State of play"),
                        text.index("### Accomplished"))
        self.assertLess(text.index("### Accomplished"),
                        text.index("## Point of failure"))


class TestTelemetryCrossCheck(_TempHarness):
    def test_unrecorded_passing_row_flags_disagreement(self):
        self._make_task(state={"checkpointed_stages": ["spec"],
                               "checkpointed_slices": ["1"]})
        self._write_rows(
            _row(stage="slice_implement", slice="1"),
            _row(stage="slice_implement", slice="7"),
            _row(stage="slice_review", slice="7", verdict="kickback",
                 outcome="kickback"),
        )
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn("**Telemetry cross-check:** session #2, stage "
                      "`slice_implement` slice `7`", text)
        self.assertIn(f"! {DISAGREEMENT_NOTE}", text)
        # The state file stays authoritative for the headline and resume.
        self.assertIn("**Last successful checkpoint:** spec / slices 1", text)
        self.assertIn("would restart at stage `slices`, slice `2`.", text)

    def test_slice_row_covered_by_state_does_not_disagree(self):
        self._make_task(state={"checkpointed_stages": ["spec", "slicing"],
                               "checkpointed_slices": ["1"]})
        self._write_rows(_row(slice="1"))
        _, text = self._report()
        self.assertNotIn(DISAGREEMENT_NOTE, text)
        self.assertNotIn("**Telemetry cross-check:**", text)

    def test_slice_less_row_maps_to_checkpointed_stage(self):
        self._make_task(state={"checkpointed_stages": ["spec"]})
        self._write_rows(_row(stage="spec_author", slice=None))
        _, text = self._report()
        self.assertNotIn(DISAGREEMENT_NOTE, text)

    def test_slice_less_row_outside_state_disagrees(self):
        self._make_task(state={"checkpointed_stages": ["spec"]})
        self._write_rows(_row(stage="feasibility", slice=None))
        _, text = self._report()
        self.assertIn("**Telemetry cross-check:** session #1, stage "
                      "`feasibility`", text)
        self.assertIn(f"! {DISAGREEMENT_NOTE}", text)

    def test_empty_state_with_passing_telemetry_uses_telemetry(self):
        self._make_task()
        self._write_rows(_row(stage="feasibility", slice=None))
        _, text = self._report()
        self.assertIn("**Last successful checkpoint:** session #1, stage "
                      "`feasibility` (from telemetry alone)", text)
        self.assertIn(f"! {DISAGREEMENT_NOTE}", text)
        accomplished = text.split("### Accomplished", 1)[1]
        self.assertIn("- session #1, stage `feasibility` (state file "
                      "records no checkpoint)", accomplished)


class TestEmptyCheckpoints(_TempHarness):
    def test_no_checkpoint_anywhere_reports_the_spec_failure(self):
        self._make_task()
        self._write_rows(
            _row(verdict="kickback", outcome="kickback"),
            _row(verdict="fail", outcome="fail"),
        )
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn(f"**Last successful checkpoint:** {NO_CHECKPOINT_TEXT}",
                      text)
        accomplished = text.split("### Accomplished", 1)[1]
        self.assertIn(f"- {NO_CHECKPOINT_TEXT}", accomplished)
        self.assertIn(f"A `harness.py resume {self.TASK_ID}` would restart "
                      "at stage `spec`.", text)
        self.assertNotIn(DISAGREEMENT_NOTE, text)


class TestCorruptTaskJson(_TempHarness):
    def _corrupt(self, payload: str) -> None:
        task_dir = self._make_task()
        (task_dir / "task.json").write_text(payload, encoding="utf-8")

    def test_unparseable_json_falls_back_to_telemetry(self):
        self._corrupt("{not json at all")
        self._write_rows(_row(stage="feasibility", slice=None))
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn(f"! {UNREADABLE_STATE_NOTE}", text)
        self.assertIn("**Status:** parked", text)
        self.assertIn("**Last successful checkpoint:** session #1, stage "
                      "`feasibility` (from telemetry alone)", text)

    def test_json_non_object_is_treated_as_unreadable(self):
        self._corrupt("[1, 2, 3]")
        self._write_rows(_row())
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn(f"! {UNREADABLE_STATE_NOTE}", text)

    def test_unreadable_state_without_telemetry_shows_no_checkpoint(self):
        self._corrupt("{broken")
        rc, text = self._report()
        self.assertEqual(0, rc)
        self.assertIn(f"! {UNREADABLE_STATE_NOTE}", text)
        self.assertIn(f"**Last successful checkpoint:** {NO_CHECKPOINT_TEXT}",
                      text)


class TestCheckpointBuilderUnit(unittest.TestCase):
    """The §6 builder at the data edge, without files or the CLI."""

    class _State:
        def __init__(self, stages, slices):
            self.checkpointed_stages = stages
            self.checkpointed_slices = slices

    def test_stages_are_ordered_per_checkpoint_order(self):
        report = build_checkpoint_report(
            self._State([CheckpointStage.SLICES, CheckpointStage.SPEC], []),
            [])
        self.assertEqual([CheckpointStage.SPEC, CheckpointStage.SLICES],
                         report.stages)

    def test_resume_slice_is_the_lowest_unused_number(self):
        report = build_checkpoint_report(
            self._State([CheckpointStage.SPEC], ["1", "3"]), [])
        self.assertEqual(CheckpointStage.SLICES, report.resume_stage)
        self.assertEqual("2", report.resume_slice)

    def test_all_stages_checkpointed_leaves_no_resume_stage(self):
        report = build_checkpoint_report(
            self._State(list(CheckpointStage), []), [])
        self.assertIsNone(report.resume_stage)
        self.assertIsNone(report.resume_slice)

    def test_no_state_no_rows_is_an_empty_checkpoint(self):
        report = build_checkpoint_report(None, [])
        self.assertEqual([], report.stages)
        self.assertIsNone(report.telemetry)
        self.assertFalse(report.disagree)
        self.assertEqual(CheckpointStage.SPEC, report.resume_stage)

    def test_latest_passing_row_wins(self):
        report = build_checkpoint_report(
            None,
            [_row(stage="slicing", slice=None, verdict="resliced",
                  outcome="pass"),
             _row(stage="slice_implement", slice="1", verdict="kickback",
                  outcome="kickback")])
        self.assertIsNotNone(report.telemetry)
        self.assertEqual(1, report.telemetry.session_index)
        self.assertEqual("slicing", report.telemetry.stage)


if __name__ == "__main__":
    unittest.main()
