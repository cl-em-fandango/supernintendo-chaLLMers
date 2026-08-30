"""Slice 4 of the `board` command: color, wide layout, terminal hardening.

Pins the ANSI color scheme (user green, auto magenta, `failed`/`parked`
warning accents — spec FR-5) gated on a TTY stdout with `NO_COLOR` unset
(FR-6, AC 4); the side-by-side column layout at wide terminals and the
stacked fallback at narrow ones, both carrying identical content (FR-6);
truncation instead of wrapping (FR-6); and an encoding-safe output path in
which non-ASCII ids and box-drawing characters can never raise
`UnicodeEncodeError` (FR-7).

Renderer-level tests call the pure `render_board` with an explicit
`RenderContext`; handler-level tests reuse the `_WiredFixture` pattern of
the earlier slice tests (temp queue dirs, `build()` patched, fake streams
standing in for stdout — the real work tree and terminal are never opened).
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core.board import (COLOR_GREEN, COLOR_MAGENTA, COLOR_RED,  # noqa: E402
                                COLOR_YELLOW, COLOR_RESET, BoardSummary, BoardTask,
                                EMPTY_COLUMN_MARKER, LocationBoard,
                                RenderContext, TaskOrigin, TRUNCATION_MARKER,
                                WIDE_LAYOUT_MIN_WIDTH, render_board,
                                write_board)
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402

ANSI_ESCAPE = re.compile(r"\033\[[0-9;]*m")


def _summary() -> BoardSummary:
    """A fixed seven-location board: user and auto tasks, empty columns."""
    return BoardSummary(locations=(
        LocationBoard("pending", (BoardTask("queued", TaskOrigin.USER),)),
        LocationBoard("claimed", ()),
        LocationBoard("active", (BoardTask("worker", TaskOrigin.USER),
                                 BoardTask("auto-gen", TaskOrigin.AUTO))),
        LocationBoard("review", ()),
        LocationBoard("parked", (BoardTask("stuck", TaskOrigin.USER),)),
        LocationBoard("failed", (BoardTask("auto-crash", TaskOrigin.AUTO),)),
        LocationBoard("done", (BoardTask("finished", TaskOrigin.USER),)),
    ))


class ColorTest(unittest.TestCase):
    """FR-5/FR-6 at the renderer: escapes follow the context, nothing else."""

    def test_user_rows_carry_green_and_auto_rows_magenta(self):
        out = render_board(_summary(), RenderContext(use_color=True, width=110))
        self.assertIn(f"{COLOR_GREEN}  worker [user]", out)
        self.assertIn(f"{COLOR_GREEN}  queued [user]", out)
        self.assertIn(f"{COLOR_MAGENTA}  auto-gen [auto]", out)
        self.assertIn(f"{COLOR_MAGENTA}  auto-crash [auto]", out)

    def test_warning_locations_accent_the_summary_counts(self):
        out = render_board(_summary(), RenderContext(use_color=True, width=110))
        self.assertIn(f"{COLOR_RED}failed 1{COLOR_RESET}", out)
        self.assertIn(f"{COLOR_YELLOW}parked 1{COLOR_RESET}", out)
        self.assertIn(f"{COLOR_GREEN}done 1{COLOR_RESET}", out)

    def test_stripping_the_escapes_yields_the_plain_output_byte_for_byte(self):
        colored = render_board(_summary(),
                               RenderContext(use_color=True, width=140))
        plain = render_board(_summary(),
                             RenderContext(use_color=False, width=140))
        self.assertEqual(ANSI_ESCAPE.sub("", colored), plain)

    def test_a_context_without_color_emits_no_escapes_at_all(self):
        for width in (80, 140):
            out = render_board(_summary(), RenderContext(width=width))
            self.assertNotIn("\033", out)

    def test_meaning_survives_without_color(self):
        out = render_board(_summary(), RenderContext(width=110))
        self.assertIn("worker [user]", out)
        self.assertIn("auto-gen [auto]", out)
        self.assertIn("failed 1", out)


class LayoutTest(unittest.TestCase):
    """FR-6: columns at wide widths, stacked sections below, same content."""

    def test_wide_width_renders_location_headers_side_by_side(self):
        out = render_board(_summary(),
                           RenderContext(width=WIDE_LAYOUT_MIN_WIDTH + 20))
        wide_line = next(line for line in out.splitlines()
                         if "pending (1)" in line)
        self.assertIn("done (1)", wide_line)
        self.assertIn("claimed (0)", wide_line)
        self.assertNotIn("──", out)

    def test_narrow_width_stacks_one_section_per_location(self):
        out = render_board(_summary(), RenderContext(width=80))
        for location in ("pending", "claimed", "active", "done"):
            self.assertIn(f"── {location} (", out)

    def test_the_same_tasks_appear_in_both_layouts(self):
        wide = ANSI_ESCAPE.sub(
            "", render_board(_summary(), RenderContext(width=140)))
        stacked = ANSI_ESCAPE.sub(
            "", render_board(_summary(), RenderContext(width=80)))
        for text in ("queued [user]", "worker [user]", "auto-gen [auto]",
                     "stuck [user]", "auto-crash [auto]", "finished [user]",
                     f"claimed 0", EMPTY_COLUMN_MARKER):
            self.assertIn(text, wide)
            self.assertIn(text, stacked)
        counts = "pending 1 · claimed 0 · active 2 · review 0 · " \
                 "parked 1 · failed 1 · done 1"
        self.assertIn(counts, wide)
        self.assertIn(counts, stacked)

    def test_long_lines_truncate_to_the_width_instead_of_wrapping(self):
        summary = BoardSummary(locations=(
            LocationBoard("active", (BoardTask("x" * 200, TaskOrigin.USER),)),
        ) + tuple(
            LocationBoard(name) for name in
            ("pending", "claimed", "review", "parked", "failed", "done")))
        out = render_board(summary, RenderContext(width=60))
        task_lines = [line for line in out.splitlines() if "xxxxx" in line]
        self.assertEqual(len(task_lines), 1)
        self.assertLessEqual(len(task_lines[0]), 60)
        self.assertTrue(task_lines[0].endswith(TRUNCATION_MARKER))

    def test_the_wide_board_never_exceeds_the_terminal_width(self):
        long_task = BoardTask("y" * 300, TaskOrigin.AUTO)
        summary = BoardSummary(locations=tuple(
            LocationBoard(name, (long_task,) if name == "active" else ())
            for name in ("pending", "claimed", "active", "review", "parked",
                         "failed", "done")))
        out = render_board(summary, RenderContext(width=140))
        for line in out.splitlines()[2:]:  # body lines, not the summary
            self.assertLessEqual(len(ANSI_ESCAPE.sub("", line)), 140)


class _StreamFixture(unittest.TestCase):
    """A temp queue with `build()` patched; stdout is a caller-supplied stream."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="board-s4-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        active = self.dir / "active"
        active.mkdir()
        (active / "worker").mkdir()
        (active / "auto-gen").mkdir()
        cfg = types.SimpleNamespace(queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        provider = DirectoryTaskProvider(self.pending, self.claimed,
                                         log=lambda line="": None)
        wired = (cfg, StatsStore(cfg.stats_path), None, provider, None,
                 lambda line="": None)
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, stream, env: dict) -> str:
        """`cmd_board` with `stream` as stdout and `env` applied; returns rc."""
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(sys, "stdout", stream):
            if "NO_COLOR" not in env:
                os.environ.pop("NO_COLOR", None)
            return handlers.cmd_board()


class FakeTTY(io.StringIO):
    """An in-memory stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class ColorGatingTest(_StreamFixture):
    """AC 4: the TTY-and-NO_COLOR decision lives in the handler."""

    def test_a_tty_without_no_color_prints_green_and_magenta_escapes(self):
        tty = FakeTTY()
        self.assertEqual(self._run(tty, {"COLUMNS": "110"}), 0)
        out = tty.getvalue()
        self.assertIn(f"{COLOR_GREEN}  worker [user]", out)
        self.assertIn(f"{COLOR_MAGENTA}  auto-gen [auto]", out)

    def test_no_color_suppresses_every_escape_on_a_tty(self):
        tty = FakeTTY()
        self.assertEqual(self._run(tty, {"COLUMNS": "110", "NO_COLOR": "1"}), 0)
        self.assertNotIn("\033", tty.getvalue())

    def test_a_non_tty_stdout_gets_no_escapes(self):
        buf = io.StringIO()
        self.assertEqual(self._run(buf, {"COLUMNS": "110"}), 0)
        self.assertNotIn("\033", buf.getvalue())

    def test_color_content_matches_the_plain_content(self):
        tty = FakeTTY()
        self._run(tty, {"COLUMNS": "110"})
        plain = io.StringIO()
        self._run(plain, {"COLUMNS": "110", "NO_COLOR": "1"})
        self.assertEqual(ANSI_ESCAPE.sub("", tty.getvalue()),
                         plain.getvalue())


class TerminalWidthTest(_StreamFixture):
    """Width reaches the renderer from the environment (FR-6)."""

    def test_a_wide_stdout_renders_columns(self):
        buf = io.StringIO()
        self._run(buf, {"COLUMNS": "140"})
        out = buf.getvalue()
        wide_line = next(line for line in out.splitlines()
                         if "pending (0)" in line)
        self.assertIn("done (0)", wide_line)

    def test_a_narrow_stdout_stacks_sections(self):
        buf = io.StringIO()
        self._run(buf, {"COLUMNS": "100"})
        self.assertIn("── pending (0)", buf.getvalue())


class EncodingSafetyTest(_StreamFixture):
    """FR-7: no `UnicodeEncodeError` traceback on a non-UTF8 stream."""

    def test_non_ascii_task_ids_render_without_a_crash(self):
        (self.dir / "active" / "tâche-ünïcode").mkdir()
        buf = io.StringIO()
        self.assertEqual(self._run(buf, {"COLUMNS": "110"}), 0)
        self.assertIn("tâche-ünïcode", buf.getvalue())

    def test_write_board_replaces_what_an_ascii_stream_cannot_hold(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        write_board("── done · ünïcode ✓", stream)  # must not raise
        stream.flush()
        data = raw.getvalue().decode("ascii")
        self.assertIn("done", data)
        self.assertIn("?", data)

    def test_write_board_leaves_a_utf8_stream_byte_identical(self):
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
        text = "── done · ünïcode ✓"
        write_board(text, stream)
        stream.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), text + "\n")

    def test_an_ascii_stdout_survives_the_whole_board(self):
        (self.dir / "active" / "tâche-ünïcode").mkdir()
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="ascii", errors="strict")
        with mock.patch.dict(os.environ, {"COLUMNS": "110"}, clear=False), \
                mock.patch.object(sys, "stdout", stream):
            os.environ.pop("NO_COLOR", None)
            self.assertEqual(handlers.cmd_board(), 0)
        stream.flush()
        self.assertIn(b"worker", raw.getvalue())


if __name__ == "__main__":
    unittest.main()
