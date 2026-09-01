"""Slice 3 of the `board` command: per-task state enrichment.

Pins the state fields on each task row: `stage=`/`done:[...]`/`updated=` from
`task.json` (spec AC 2), the claim owner with `OWNER_UNKNOWN` rendered `?`,
the collapsed per-task stats line (`sessions`/`tokens`/`time`/`last verdict`
per FR-3, absent for tasks with no rows), the best-effort terminal reason
for `parked/`/`failed/`, and the rule that stats rows for task ids in no
queue location stay off the board. The board stays read-only (AC 8).

Same `_WiredFixture` pattern as test_board_body.py: temp queue dirs and
`build()` patched, so the real work tree is never opened.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core.claim_metadata import write_metadata  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import SessionRecord, StatsStore  # noqa: E402


class _WiredFixture(unittest.TestCase):
    """A temp queue with `build()` patched onto it; the board runs in-process."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="board-s3-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)
        cfg = types.SimpleNamespace(work_dir=self.dir,
                                    queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, self.provider, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Pin the terminal width: stacked layout (< 120 cells), wide enough
        # that no board line truncates, so assertions see full text.
        env = mock.patch.dict(os.environ, {"COLUMNS": "110"})
        env.start()
        self.addCleanup(env.stop)
        self.store: StatsStore = wired[1]

    def _board(self) -> str:
        """Run `cmd_board`, return what it printed (asserts exit 0)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = handlers.cmd_board()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _make_dir_task(self, location: str, task_id: str,
                       state: dict | None = None) -> Path:
        """A directory-shaped task under `location` with an optional task.json."""
        task_dir = self.dir / location / task_id
        task_dir.mkdir(parents=True)
        if state is not None:
            (task_dir / "task.json").write_text(json.dumps(
                {"id": task_id, "status": "active", **state}))
        return task_dir

    def _record(self, task_id: str, *, verdict: str = "unknown",
                outcome: str = "unknown", ts: str = "",
                peak_tokens: int = 0, duration_s: float = 0.0) -> None:
        self.store.record(SessionRecord(
            ts=ts, task_id=task_id, stage="spec_author", model="m",
            verdict=verdict, outcome=outcome, peak_tokens=peak_tokens,
            duration_s=duration_s, rc=0))

    def _section_body(self, out: str, location: str) -> str:
        """The task lines rendered under one location's header."""
        lines = out.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.startswith(f"── {location} ("))
        body = []
        for line in lines[start + 1:]:
            if line.startswith("── "):
                break
            body.append(line)
        return "\n".join(body)


class ActiveStateTest(_WiredFixture):
    """A populated `task.json` shows stage, checkpoints and last_updated (AC 2)."""

    def test_active_task_shows_stage_checkpoint_list_and_last_updated(self):
        self._make_dir_task("active", "worker", {
            "stage": "slicing",
            "checkpointed_stages": ["spec", "feasibility"],
            "last_updated": "2026-03-01T10:00:00+00:00",
        })
        body = self._section_body(self._board(), "active")
        self.assertIn("worker [user]", body)
        self.assertIn("stage=slicing done:[spec,feasibility]", body)
        self.assertIn("updated=2026-03-01T10:00:00+00:00", body)

    def test_empty_checkpoint_list_renders_as_empty_brackets(self):
        self._make_dir_task("active", "fresh", {
            "stage": "spec", "checkpointed_stages": [],
            "last_updated": "2026-03-01T10:00:00+00:00",
        })
        body = self._section_body(self._board(), "active")
        self.assertIn("stage=spec done:[]", body)

    def test_task_json_without_a_stage_shows_no_stage_field(self):
        self._make_dir_task("active", "bare", {"last_updated": "x"})
        body = self._section_body(self._board(), "active")
        self.assertIn("bare [user]", body)
        self.assertNotIn("stage=", body)

    def test_last_updated_falls_back_to_the_entry_mtime(self):
        task_dir = self._make_dir_task("active", "no-stamp")  # no task.json
        os.utime(task_dir, (1_700_000_000, 1_700_000_000))
        body = self._section_body(self._board(), "active")
        self.assertIn("updated=2023-11-14T22:13:20+00:00", body)


class ClaimOwnerTest(_WiredFixture):
    """`claimed/` entries show the sidecar owner; unknown reads `?` (FR-3)."""

    def _claim(self, name: str) -> Path:
        claim_file = self.claimed / f"{name}.md"
        claim_file.write_text(f"# {name}\n")
        return claim_file

    def test_a_claim_with_a_sidecar_shows_its_owner(self):
        claim_file = self._claim("held")
        write_metadata(claim_file, owner="run-7-deadbeef")
        body = self._section_body(self._board(), "claimed")
        self.assertIn("held [user]", body)
        self.assertIn("owner=run-7-deadbeef", body)

    def test_a_claim_without_a_sidecar_shows_a_question_mark(self):
        self._claim("orphan")
        body = self._section_body(self._board(), "claimed")
        self.assertIn("orphan [user]", body)
        self.assertIn("owner=?", body)

    def test_a_corrupt_sidecar_shows_a_question_mark(self):
        claim_file = self._claim("broken")
        claim_file.with_name(claim_file.name + ".claim.json").write_text("{junk")
        body = self._section_body(self._board(), "claimed")
        self.assertIn("broken [user]", body)
        self.assertIn("owner=?", body)


class TaskStatsLineTest(_WiredFixture):
    """Session rows collapse into one line per task (FR-3)."""

    def test_rows_collapse_into_sessions_tokens_time_and_newest_verdict(self):
        # A short `last_updated` keeps the row inside the pinned 110-column
        # width, so truncation (slice 4) cannot cut the asserted fields.
        self._make_dir_task("active", "worker",
                            {"last_updated": "2026-01-01T00:00:00"})
        # The newer-ts row is appended FIRST: recency must follow `ts`, not
        # append order, so the last verdict is the 01-02 row's `fail`.
        self._record("worker", verdict="fail", outcome="fail",
                     ts="2026-01-02T00:00:00", peak_tokens=200, duration_s=20.0)
        self._record("worker", verdict="pass", outcome="pass",
                     ts="2026-01-01T00:00:00", peak_tokens=100, duration_s=10.0)
        body = self._section_body(self._board(), "active")
        self.assertIn("sessions=2 tokens=300 time=30s last verdict=fail", body)

    def test_rows_without_timestamps_fall_back_to_append_order(self):
        self._make_dir_task("active", "worker",
                            {"last_updated": "2026-01-01T00:00:00"})
        self._record("worker", verdict="kickback")
        self._record("worker", verdict="pass")
        body = self._section_body(self._board(), "active")
        self.assertIn("last verdict=pass", body)

    def test_a_task_with_no_rows_shows_no_stats_fields(self):
        self._make_dir_task("active", "quiet")
        body = self._section_body(self._board(), "active")
        self.assertIn("quiet [user]", body)
        self.assertNotIn("sessions=", body)
        self.assertNotIn("tokens=", body)
        self.assertNotIn("last verdict=", body)

    def test_unreadable_numbers_count_as_zero_not_a_crash(self):
        self._make_dir_task("active", "worker",
                            {"last_updated": "2026-01-01T00:00:00"})
        self.store.path.write_text(json.dumps(
            {"task_id": "worker", "verdict": "weird-value",
             "peak_tokens": "junk", "duration_s": "nope"}) + "\n")
        body = self._section_body(self._board(), "active")
        self.assertIn("sessions=1 tokens=0 time=0s last verdict=weird-value",
                      body)

    def test_stats_rows_for_absent_task_ids_stay_off_the_board(self):
        self._record("ghost-task", verdict="pass")
        self._make_dir_task("active", "real")
        out = self._board()
        self.assertNotIn("ghost-task", out)
        self.assertIn("real [user]", out)


class TerminalReasonTest(_WiredFixture):
    """`parked/`/`failed/` show the recorded reason best-effort (FR-3)."""

    def _review_summary(self, task_id: str, summary: str) -> None:
        review = self.dir / "review"
        review.mkdir(exist_ok=True)
        (review / f"{task_id}.md").write_text(
            f"# Task: {task_id}\n\n**Status:** PARKED\n\n"
            f"## Executive summary\n\n{summary}\n\n## Artifacts\n\n- spec\n")

    def test_a_parked_task_shows_the_reason_from_its_review_summary(self):
        self._make_dir_task("parked", "stuck")
        self._review_summary("stuck", "over context budget on slice 2")
        body = self._section_body(self._board(), "parked")
        self.assertIn("reason=over context budget on slice 2", body)

    def test_a_failed_task_without_any_record_shows_no_reason_and_exits_0(self):
        self._make_dir_task("failed", "gone")
        body = self._section_body(self._board(), "failed")
        self.assertIn("gone [user]", body)
        self.assertNotIn("reason=", body)

    def test_a_review_summary_without_the_section_is_not_a_reason(self):
        self._make_dir_task("parked", "odd")
        review = self.dir / "review"
        review.mkdir()
        (review / "odd.md").write_text("# Task: odd\nno sections here\n")
        body = self._section_body(self._board(), "parked")
        self.assertNotIn("reason=", body)

    def test_locations_other_than_parked_and_failed_show_no_reason(self):
        self._make_dir_task("active", "working")
        self._review_summary("working", "should not be picked up")
        body = self._section_body(self._board(), "active")
        self.assertNotIn("reason=", body)


class ReadOnlyTest(_WiredFixture):
    """Rendering state changes nothing on disk (AC 8)."""

    def test_the_queue_tree_is_identical_before_and_after_a_board_run(self):
        self._make_dir_task("active", "worker", {"stage": "slicing"})
        self._make_dir_task("parked", "stuck")
        claim_file = self.claimed / "held.md"
        claim_file.write_text("# held\n")
        write_metadata(claim_file, owner="run-1-x")
        self._record("worker", verdict="pass", ts="2026-01-01T00:00:00")

        def tree() -> list[tuple[str, int]]:
            return sorted((str(p.relative_to(self.dir)),
                           p.stat().st_mtime_ns if p.is_file() else -1)
                          for p in self.dir.rglob("*") if "logs" not in p.parts)

        before = tree()
        self._board()
        self.assertEqual(tree(), before)


if __name__ == "__main__":
    unittest.main()
