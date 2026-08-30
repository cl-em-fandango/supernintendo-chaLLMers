"""Slice 1 of the `board` command: subcommand wiring + executive summary.

Pins the plain-text executive summary over an empty-body board: per-location
counts in lifecycle order (spec AC 1), the stranded-claims warning (AC 5),
the aggregate stats line with its omission rules (FR-2), the all-empty queue
(FR-7), and the hidden `kanban` alias producing byte-identical output (AC 9).
Task rows, color and layout are later slices.

Every fixture is a temp dir with `build()` patched, so the real work tree is
never opened (the `_WiredFixture` pattern from test_handlers_claims.py).
"""
from __future__ import annotations

import importlib.util
import io
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
from harness.cli.parser import parse_args  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import SessionRecord, StatsStore  # noqa: E402
from harness.workflow.task_lifecycle import QUEUE_LOCATIONS_ALL  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


class _WiredFixture(unittest.TestCase):
    """A temp queue, the real directory provider and stats store, `build()` wired."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="board-s1-"))
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
        # Pin the terminal width: stacked layout (< 120 cells), wide enough
        # that no board line truncates, so assertions see full text.
        env = mock.patch.dict(os.environ, {"COLUMNS": "110"})
        env.start()
        self.addCleanup(env.stop)
        self.wired = wired

    def _board(self) -> str:
        """Run `cmd_board`, return what it printed (asserts exit 0)."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = handlers.cmd_board()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _make_location(self, name: str, *task_names: str) -> None:
        """One queue location holding the named task dirs (files for pending/review)."""
        loc = self.dir / name
        loc.mkdir(exist_ok=True)
        for task in task_names:
            if name in ("pending", "review"):
                (loc / f"{task}.md").write_text(f"# {task}\n")
            else:
                (loc / task).mkdir()

    def _record(self, outcome: str, *, task_id: str = "t", peak_tokens: int = 0,
                verdict: str = "unknown") -> None:
        self.wired[1].record(SessionRecord(
            ts="2026-01-01T00:00:00", task_id=task_id, stage="spec_author",
            model="m", verdict=verdict, outcome=outcome,
            peak_tokens=peak_tokens, duration_s=1.0, rc=0))


class SummaryCountsTest(_WiredFixture):
    """The per-location counts line, in lifecycle order (AC 1)."""

    def test_a_known_mix_across_all_seven_locations_counts_correctly(self):
        self._make_location("pending", "p1", "p2")
        (self.claimed / "c1.md").write_text("# c1\n")
        self._make_location("active", "a1")
        self._make_location("review", "r1", "r2", "r3")
        self._make_location("parked")
        self._make_location("failed", "f1")
        self._make_location("done", "d1", "d2", "d3", "d4")
        out = self._board()
        self.assertIn("pending 2 · claimed 1 · active 1 · review 3 · "
                      "parked 0 · failed 1 · done 4", out)

    def test_the_counts_line_lists_locations_in_lifecycle_order(self):
        out = self._board()
        counts_line = next(line for line in out.splitlines()
                           if line.startswith("pending "))
        listed = [part.rsplit(" ", 1)[0] for part in counts_line.split(" · ")]
        self.assertEqual(listed, list(QUEUE_LOCATIONS_ALL))

    def test_ownership_sidecars_are_never_counted_as_tasks(self):
        (self.claimed / "c1.md").write_text("# c1\n")
        (self.claimed / "c1.md.claim.json").write_text('{"owner": "x"}')
        self.assertIn("claimed 1", self._board())


class EmptyQueueTest(_WiredFixture):
    """The all-empty queue: zero header, one `-` per column (FR-7)."""

    def test_an_empty_queue_renders_zero_counts_and_a_dash_per_column(self):
        out = self._board()
        self.assertIn("pending 0 · claimed 0 · active 0 · review 0 · "
                      "parked 0 · failed 0 · done 0", out)
        self.assertEqual(out.count("  -"), len(QUEUE_LOCATIONS_ALL))

    def test_every_location_keeps_a_header_even_when_empty(self):
        out = self._board()
        for location in QUEUE_LOCATIONS_ALL:
            self.assertIn(f"{location} (0)", out)


class StrandedClaimsTest(_WiredFixture):
    """The requeue-claims warning appears exactly when `claimed/` is populated (AC 5)."""

    def test_a_populated_claimed_shows_the_requeue_claims_warning(self):
        (self.claimed / "c1.md").write_text("# c1\n")
        (self.claimed / "c2.md").write_text("# c2\n")
        out = self._board()
        self.assertIn("2 claimed tasks", out)
        self.assertIn("requeue-claims", out)

    def test_an_empty_claimed_shows_no_warning(self):
        out = self._board()
        self.assertNotIn("claimed tasks", out)
        self.assertNotIn("requeue-claims", out)


class AggregateStatsLineTest(_WiredFixture):
    """The stats line: sessions, pass %, reject/kickout %, tokens (FR-2)."""

    def test_the_line_aggregates_over_every_row_in_the_store(self):
        self._record("pass", peak_tokens=1000)
        self._record("pass", peak_tokens=1000)
        self._record("fail", peak_tokens=1000)
        self._record("kickback", peak_tokens=1000)
        self._record("error", peak_tokens=1000)     # undecided, tokens still count
        self._record("done", peak_tokens=1000)      # undecided per FR-2
        self.assertIn("sessions 6 · pass 50% · reject/kickout 50% · tokens 6000",
                      self._board())

    def test_an_empty_store_omits_the_line(self):
        out = self._board()
        self.assertNotIn("sessions ", out)
        self.assertNotIn("tokens ", out)

    def test_a_store_with_no_decided_sessions_omits_the_line_without_error(self):
        self._record("error", peak_tokens=10)
        self._record("progress", peak_tokens=10)
        out = self._board()
        self.assertNotIn("reject/kickout", out)

    def test_all_pass_and_all_reject_read_as_hundred_percent(self):
        self._record("pass")
        self.assertIn("pass 100% · reject/kickout 0%", self._board())
        self._record("kickout")
        self.assertIn("pass 50% · reject/kickout 50%", self._board())


class KanbanAliasTest(_WiredFixture):
    """`kanban` is a hidden alias whose output is byte-identical to `board` (AC 9)."""

    def _load_script(self):
        spec = importlib.util.spec_from_file_location(
            "harness_script", REPO_ROOT / "harness.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run_script(self, command: str) -> tuple[int, str]:
        module = self._load_script()
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["harness.py", command]), \
                redirect_stdout(buf):
            rc = module.main()
        return rc, buf.getvalue()

    def test_both_subcommands_parse(self):
        self.assertEqual(parse_args(["board"]).command, "board")
        self.assertEqual(parse_args(["kanban"]).command, "kanban")

    def test_kanban_output_is_byte_identical_to_board(self):
        self._make_location("active", "a1")
        rc_board, board_out = self._run_script("board")
        rc_kanban, kanban_out = self._run_script("kanban")
        self.assertEqual((rc_board, rc_kanban), (0, 0))
        self.assertEqual(board_out, kanban_out)

    def test_kanban_is_hidden_from_help(self):
        """`help=argparse.SUPPRESS` on the alias, the `requeue` pattern."""
        import argparse

        from harness.cli.parser import build_parser
        parser = build_parser()
        subaction = next(a for a in parser._actions
                         if isinstance(a, argparse._SubParsersAction))
        helps = {a.dest: a.help for a in subaction._choices_actions}
        self.assertNotEqual(helps["board"], argparse.SUPPRESS)
        self.assertEqual(helps["kanban"], argparse.SUPPRESS)
        self.assertEqual(helps["requeue"], argparse.SUPPRESS)


if __name__ == "__main__":
    unittest.main()
