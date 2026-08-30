"""Slice 2 of the `board` command: the board body (stacked sections).

Pins the per-task rows: id + `auto`/`user` origin tag classified by the
`auto-` id prefix (spec AC 3, FR-4), the deterministic ordering of spec §6
(`last_updated` descending, tasks without one last, ties by id ascending),
the `done/` cap of 10 plus `(+N more)` (AC 7), and the corrupt/missing
`task.json` resilience (`state: unknown`, exit 0 — AC 6). The empty-queue
`-` markers from slice 1 must survive (FR-7).

Same `_WiredFixture` pattern as test_board_summary.py: temp queue dirs and
`build()` patched, so the real work tree is never opened.
"""
from __future__ import annotations

import io
import json
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
from harness.core.board import AUTO_ID_PREFIX, DONE_DISPLAY_CAP  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402
from harness.workflow.task_lifecycle import QUEUE_LOCATIONS_ALL  # noqa: E402


class _WiredFixture(unittest.TestCase):
    """A temp queue with `build()` patched onto it; the board runs in-process."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="board-s2-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)
        cfg = types.SimpleNamespace(queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, self.provider, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _board(self) -> str:
        """Run `cmd_board`, return what it printed (asserts exit 0)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = handlers.cmd_board()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _make_dir_task(self, location: str, task_id: str, *,
                       last_updated: str | None = None,
                       corrupt: bool = False,
                       no_task_json: bool = False) -> Path:
        """A directory-shaped task under `location`, with a controllable task.json."""
        task_dir = self.dir / location / task_id
        task_dir.mkdir(parents=True)
        if no_task_json:
            return task_dir
        path = task_dir / "task.json"
        if corrupt:
            path.write_text("{not json at all")
        else:
            state = {"id": task_id, "status": "active"}
            if last_updated is not None:
                state["last_updated"] = last_updated
            path.write_text(json.dumps(state))
        return task_dir

    def _section_body(self, out: str, location: str) -> list[str]:
        """The task lines rendered under one location's header."""
        lines = out.splitlines()
        start = next(i for i, line in enumerate(lines)
                     if line.startswith(f"── {location} ("))
        body = []
        for line in lines[start + 1:]:
            if line.startswith("── "):
                break
            body.append(line)
        return body

    def _section_ids(self, out: str, location: str) -> list[str]:
        ids = []
        for line in self._section_body(out, location):
            stripped = line.strip()
            if stripped and stripped != "-" and not stripped.startswith("("):
                ids.append(stripped.split(" ", 1)[0])
        return ids


class OriginTagTest(_WiredFixture):
    """Every task shows id + `auto`/`user`, classified by the id prefix (AC 3)."""

    def test_each_of_the_seven_sections_lists_its_tasks_with_an_origin_tag(self):
        (self.pending / "p-user.md").write_text("# p\n")
        (self.pending / f"{AUTO_ID_PREFIX}1-gen.md").write_text("# p\n")
        (self.claimed / "c-user.md").write_text("# c\n")
        (self.claimed / f"{AUTO_ID_PREFIX}2-gen.md").write_text("# c\n")
        self._make_dir_task("active", "a-user")
        self._make_dir_task("active", f"{AUTO_ID_PREFIX}4-gen")
        (self.dir / "review").mkdir()
        (self.dir / "review" / "r-user.md").write_text("# r\n")
        (self.dir / "review" / f"{AUTO_ID_PREFIX}5-gen.md").write_text("# r\n")
        self._make_dir_task("parked", "pk-user")
        self._make_dir_task("parked", f"{AUTO_ID_PREFIX}6-gen")
        self._make_dir_task("failed", "f-user")
        self._make_dir_task("failed", f"{AUTO_ID_PREFIX}7-gen")
        self._make_dir_task("done", "d-user")
        self._make_dir_task("done", f"{AUTO_ID_PREFIX}8-gen")
        out = self._board()
        for loc, user_id, auto_id in [
            ("pending", "p-user", f"{AUTO_ID_PREFIX}1-gen"),
            ("claimed", "c-user", f"{AUTO_ID_PREFIX}2-gen"),
            ("active", "a-user", f"{AUTO_ID_PREFIX}4-gen"),
            ("review", "r-user", f"{AUTO_ID_PREFIX}5-gen"),
            ("parked", "pk-user", f"{AUTO_ID_PREFIX}6-gen"),
            ("failed", "f-user", f"{AUTO_ID_PREFIX}7-gen"),
            ("done", "d-user", f"{AUTO_ID_PREFIX}8-gen"),
        ]:
            body = "\n".join(self._section_body(out, loc))
            self.assertIn(f"{user_id} [user]", body, f"{loc}: {body!r}")
            self.assertIn(f"{auto_id} [auto]", body, f"{loc}: {body!r}")

    def test_a_task_id_that_merely_contains_auto_is_user(self):
        """Only the `auto-` *prefix* classifies; `my-auto-thing` is user."""
        self._make_dir_task("active", "my-auto-thing")
        self._make_dir_task("active", "autox")
        body = "\n".join(self._section_body(self._board(), "active"))
        self.assertIn("my-auto-thing [user]", body)
        self.assertIn("autox [user]", body)


class OrderingTest(_WiredFixture):
    """Spec §6: last_updated desc, no-timestamp tasks last, ties by id asc."""

    def test_tasks_sort_by_last_updated_descending_then_no_timestamp_then_id(self):
        self._make_dir_task("active", "b-mid",
                            last_updated="2026-02-01T00:00:00+00:00")
        self._make_dir_task("active", "a-newest",
                            last_updated="2026-03-01T00:00:00+00:00")
        self._make_dir_task("active", "c-oldest",
                            last_updated="2026-01-01T00:00:00+00:00")
        self._make_dir_task("active", "e-no-stamp", no_task_json=True)
        self._make_dir_task("active", "d-empty-stamp")  # task.json, no field
        self.assertEqual(self._section_ids(self._board(), "active"),
                         ["a-newest", "b-mid", "c-oldest", "d-empty-stamp",
                          "e-no-stamp"])

    def test_unparseable_last_updated_reads_as_no_timestamp(self):
        self._make_dir_task("active", "z-good",
                            last_updated="2026-01-01T00:00:00+00:00")
        self._make_dir_task("active", "a-junk", last_updated="yesterday")
        self.assertEqual(self._section_ids(self._board(), "active"),
                         ["z-good", "a-junk"])

    def test_two_runs_produce_identical_output(self):
        for i in range(3):
            self._make_dir_task("done", f"t{i}",
                                last_updated=f"2026-01-0{i + 1}T00:00:00+00:00")
        self._make_dir_task("done", "u1", no_task_json=True)
        self._make_dir_task("done", "u0", no_task_json=True)
        self.assertEqual(self._board(), self._board())

    def test_corrupt_task_json_sorts_with_the_no_timestamp_tasks(self):
        self._make_dir_task("active", "a-corrupt", corrupt=True)
        self._make_dir_task("active", "b-good",
                            last_updated="2026-01-01T00:00:00+00:00")
        self.assertEqual(self._section_ids(self._board(), "active"),
                         ["b-good", "a-corrupt"])


class DoneCapTest(_WiredFixture):
    """`done/` shows the 10 most recently updated tasks plus `(+N more)` (AC 7)."""

    def _fill_done(self, count: int) -> None:
        for i in range(count):
            self._make_dir_task("done", f"d{i:02}",
                                last_updated=f"2026-01-{i + 1:02d}T00:00:00+00:00")

    def test_twelve_done_tasks_render_ten_listed_and_plus_two_more(self):
        self._fill_done(12)
        body = self._section_body(self._board(), "done")
        listed = [line for line in body if "(+2 more)" not in line
                  and line.strip() not in ("", "-")]
        self.assertEqual(len(listed), 10)
        self.assertIn("(+2 more)", "\n".join(body))

    def test_the_cap_keeps_the_most_recent_tasks(self):
        self._fill_done(12)
        ids = self._section_ids(self._board(), "done")
        self.assertEqual(len(ids), DONE_DISPLAY_CAP)
        self.assertEqual(ids[0], "d11")  # newest first
        self.assertNotIn("d00", ids)    # oldest is in the hidden tail

    def test_exactly_the_cap_lists_every_task_with_no_more_line(self):
        self._fill_done(DONE_DISPLAY_CAP)
        out = self._board()
        self.assertEqual(len(self._section_ids(out, "done")), DONE_DISPLAY_CAP)
        self.assertNotIn("more)", "\n".join(self._section_body(out, "done")))

    def test_the_cap_applies_to_done_only(self):
        for i in range(12):
            self._make_dir_task("parked", f"p{i:02}")
        out = self._board()
        self.assertEqual(len(self._section_ids(out, "parked")), 12)
        self.assertNotIn("more)", "\n".join(self._section_body(out, "parked")))


class CorruptStateTest(_WiredFixture):
    """Missing/corrupt `task.json`: task listed with `state: unknown`, exit 0 (AC 6)."""

    def test_a_corrupt_task_json_still_lists_the_task_with_origin_and_unknown(self):
        self._make_dir_task("active", f"{AUTO_ID_PREFIX}9-broken", corrupt=True)
        body = "\n".join(self._section_body(self._board(), "active"))
        self.assertIn(f"{AUTO_ID_PREFIX}9-broken [auto]  state: unknown", body)

    def test_a_task_without_task_json_reports_unknown_state(self):
        self._make_dir_task("active", "plain-dir", no_task_json=True)
        body = "\n".join(self._section_body(self._board(), "active"))
        self.assertIn("plain-dir [user]  state: unknown", body)

    def test_a_task_json_that_is_not_an_object_reports_unknown_state(self):
        task_dir = self._make_dir_task("active", "list-json")
        (task_dir / "task.json").write_text("[1, 2, 3]")
        body = "\n".join(self._section_body(self._board(), "active"))
        self.assertIn("list-json [user]  state: unknown", body)

    def test_a_readable_task_json_does_not_report_unknown_state(self):
        self._make_dir_task("active", "healthy")
        body = "\n".join(self._section_body(self._board(), "active"))
        self.assertIn("healthy [user]", body)
        self.assertNotIn("unknown", body)

    def test_a_file_named_task_json_where_a_dir_is_expected_does_not_crash(self):
        (self.dir / "active").mkdir()
        (self.dir / "active" / "weird").write_text("not a dir")
        out = self._board()  # exit 0 asserted by _board()
        self.assertIn("weird [user]", out)


class EmptyQueueRegressionTest(_WiredFixture):
    """Slice 1's empty-column markers survive the body (FR-7, no regression)."""

    def test_an_empty_queue_still_renders_one_dash_per_location(self):
        out = self._board()
        self.assertEqual(out.count("  -"), len(QUEUE_LOCATIONS_ALL))
        for location in QUEUE_LOCATIONS_ALL:
            self.assertIn(f"{location} (0)", out)


if __name__ == "__main__":
    unittest.main()
