"""Tests for workflow journey analysis, ASCII graph readout, and static artifact persistence."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.stats import (
    SessionRecord,
    StatsStore,
    render_task_journey,
    task_journey_analysis,
)


class JourneyStatsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.stats_file = Path(self._tmp.name) / "stats" / "sessions.jsonl"
        self.store = StatsStore(self.stats_file)

    def _sample_journey_rows(self) -> list[dict]:
        return [
            {
                "ts": "2026-08-26T10:00:00+0000",
                "task_id": "001-test",
                "stage": "spec_author",
                "model": "writer-model",
                "verdict": "done",
                "outcome": "done",
                "peak_tokens": 12000,
                "duration_s": 15.0,
                "rc": 0,
                "iteration": 1,
                "notes": "",
            },
            {
                "ts": "2026-08-26T10:01:00+0000",
                "task_id": "001-test",
                "stage": "spec_assess_tw",
                "model": "assessor-model",
                "verdict": "kickback",
                "outcome": "kickback",
                "peak_tokens": 8000,
                "duration_s": 10.0,
                "rc": 0,
                "iteration": 1,
                "notes": "missing edge case",
            },
            {
                "ts": "2026-08-26T10:02:00+0000",
                "task_id": "001-test",
                "stage": "spec_author",
                "model": "writer-model",
                "verdict": "done",
                "outcome": "done",
                "peak_tokens": 15000,
                "duration_s": 20.0,
                "rc": 0,
                "iteration": 2,
                "notes": "retry after kickback",
            },
            {
                "ts": "2026-08-26T10:03:00+0000",
                "task_id": "001-test",
                "stage": "feasibility",
                "model": "assessor-model",
                "verdict": "pass",
                "outcome": "pass",
                "peak_tokens": 5000,
                "duration_s": 5.0,
                "rc": 0,
                "iteration": 1,
                "notes": "",
            },
            {
                "ts": "2026-08-26T10:04:00+0000",
                "task_id": "001-test",
                "stage": "slice_implement",
                "slice": "1",
                "model": "implementer-model",
                "verdict": "done",
                "outcome": "done",
                "peak_tokens": 65000,
                "duration_s": 180.0,  # Long duration hotspot
                "rc": 0,
                "iteration": 1,
                "notes": "",
            },
            {
                "ts": "2026-08-26T10:08:00+0000",
                "task_id": "001-test",
                "stage": "tech_review",
                "slice": "1",
                "model": "assessor-model",
                "verdict": "pass",
                "outcome": "pass",
                "peak_tokens": 10000,
                "duration_s": 12.0,
                "rc": 0,
                "iteration": 1,
                "notes": "",
            },
        ]

    def test_task_journey_analysis_detects_loops_and_hotspots(self):
        rows = self._sample_journey_rows()
        analysis = task_journey_analysis(rows, task_id="001-test")

        self.assertEqual(analysis.task_id, "001-test")
        self.assertEqual(analysis.total_sessions, 6)
        self.assertEqual(analysis.total_duration_s, 242.0)
        self.assertEqual(analysis.max_peak_tokens, 65000)
        self.assertEqual(analysis.bounces_count, 1)
        self.assertEqual(analysis.loops_count, 1)

        # Hotspot verification (180s slice_implement is > 70% of time)
        self.assertTrue(len(analysis.hotspots) >= 1)
        self.assertEqual(analysis.hotspots[0].stage, "slice_implement")
        self.assertTrue(analysis.hotspots[0].is_hotspot)

        # Bounce description
        self.assertIn("spec_assess_tw", analysis.bounce_descriptions[0])
        self.assertIn("KICKBACK", analysis.bounce_descriptions[0])

        # Loop description
        self.assertIn("iteration 2", analysis.loop_descriptions[0])

    def test_render_task_journey_contains_graph_and_sections(self):
        rows = self._sample_journey_rows()
        text = render_task_journey(rows, task_id="001-test")

        self.assertIn("WORKFLOW JOURNEY GRAPH: 001-test", text)
        self.assertIn("CHRONOLOGICAL JOURNEY FLOW", text)
        self.assertIn("LOOPS & RETRIES (1 detected)", text)
        self.assertIn("BLOCKAGES & BOUNCES (1 detected)", text)
        self.assertIn("TIME HOTSPOTS (Where it took ages)", text)
        self.assertIn("STAGE SUMMARY FOR TASK 001-test", text)

        # Visual symbols check
        self.assertIn("───►", text)
        self.assertIn("───┐ [BOUNCE ↩]", text)
        self.assertIn("◄──┘ [LOOP #2] ───►", text)
        self.assertIn("───► [COMPLETE ✔]", text)
        self.assertIn("🔥", text)  # Hotspot indicator

    def test_store_writes_static_journey_file(self):
        for r in self._sample_journey_rows():
            self.store.record(SessionRecord(
                ts=r["ts"],
                task_id=r["task_id"],
                stage=r["stage"],
                model=r["model"],
                verdict=r["verdict"],
                outcome=r["outcome"],
                peak_tokens=r["peak_tokens"],
                duration_s=r["duration_s"],
                rc=r["rc"],
                slice=r.get("slice"),
                iteration=r.get("iteration", 1),
                notes=r.get("notes", ""),
            ))

        journey_path = self.store.write_task_journey("001-test")
        self.assertTrue(journey_path.exists())
        content = journey_path.read_text()
        self.assertIn("WORKFLOW JOURNEY GRAPH: 001-test", content)
        self.assertIn("180.0s 🔥", content)

    def test_empty_rows_render_clean_message(self):
        text = render_task_journey([], task_id="empty-task")
        self.assertIn("No sessions recorded for task 'empty-task'", text)


if __name__ == "__main__":
    unittest.main()
