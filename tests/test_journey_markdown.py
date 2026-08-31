"""Slice 4 (001-full-interactions-logged): `artifacts/journey.md`.

The ASCII journey (`journey.txt`) links to nothing, because until this feature
no per-session artifact existed to link to. These tests pin the Markdown
journey that now sits beside it (FR-3.1/3.3/3.4, AC 5):

- summary header with the same headline metrics as the ASCII readout;
- one session-table row per stats row, whose Transcript cell is a link
  relative to `artifacts/` (`sessions/NNN-….md`) or an em dash when that
  session has no transcript;
- diagnostics (loops, blockages, hotspots, stage summary) ported as lists;
- rows are paired with transcripts by sequence *and* stage/slice/iteration, so
  a resumed task whose numbering starts past its restored rows still links
  correctly;
- `_persist_journey_readout` writes `journey.md` next to a `journey.txt` that
  is byte-identical to `render_task_journey` output (FR-4).

The end-to-end part drives a real `run_pi_session` subprocess with a fake `pi`
script on `PATH` (never the real binary — `setUp` asserts `shutil.which`
resolves inside the temp dir).

Run from the repo root:  python3 -m unittest tests.test_journey_markdown
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
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import (
    SessionRecord,
    StatsStore,
    render_task_journey,
    render_task_journey_markdown,
)
from harness.core.transcripts import list_transcripts, match_rows_to_transcripts
from harness.workflow.pipeline import Pipeline

ASSISTANT_TEXT = "All good.\n\nVERDICT: done"


def _cfg(work_dir: Path) -> Config:
    return Config(
        work_dir=work_dir,
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


def _journey_rows() -> list[dict]:
    """A kickback, a retry and a slice session — loops and bounces included."""
    return [
        {"ts": "2026-08-26T10:00:00+0000", "task_id": "001-test",
         "stage": "spec_author", "model": "writer-model", "verdict": "done",
         "outcome": "done", "peak_tokens": 12000, "duration_s": 15.0,
         "rc": 0, "iteration": 1, "notes": ""},
        {"ts": "2026-08-26T10:01:00+0000", "task_id": "001-test",
         "stage": "spec_assess_tw", "model": "assessor|model", "verdict": "kickback",
         "outcome": "kickback", "peak_tokens": 8000, "duration_s": 10.0,
         "rc": 0, "iteration": 1, "notes": "missing edge case"},
        {"ts": "2026-08-26T10:02:00+0000", "task_id": "001-test",
         "stage": "spec_author", "model": "writer-model", "verdict": "done",
         "outcome": "done", "peak_tokens": 15000, "duration_s": 20.0,
         "rc": 0, "iteration": 2, "notes": "retry after kickback"},
        {"ts": "2026-08-26T10:04:00+0000", "task_id": "001-test",
         "stage": "slice_implement", "slice": "1", "model": "implementer-model",
         "verdict": "done", "outcome": "done", "peak_tokens": 65000,
         "duration_s": 180.0, "rc": 0, "iteration": 1, "notes": ""},
    ]


def _transcript(sequence: int, stage: str, slice_id: str | None = None,
                iteration: int = 1) -> Path:
    """The path a transcript of that identity would have (content is irrelevant:
    matching reads filenames)."""
    name = f"{sequence:03d}-{stage}"
    if slice_id is not None:
        name += f"-slice-{slice_id}"
    if iteration != 1:
        name += f"-iter-{iteration}"
    return Path(f"{name}.md")


class RendererTest(unittest.TestCase):
    def test_summary_header_carries_headline_metrics(self):
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test")
        self.assertIn("# Journey: 001-test", text)
        self.assertIn("**Sessions:** 4", text)
        self.assertIn("**Wall clock:** 225.0s", text)
        self.assertIn("**Max tokens:** 65.0k", text)
        self.assertIn("**Bounces/blocks:** 1", text)
        self.assertIn("**Loops/retries:** 1", text)

    def test_table_has_one_row_per_session_with_relative_links(self):
        files = ["001-spec_author.md", "002-spec_assess_tw.md", None,
                 "004-slice_implement-slice-1.md"]
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test",
                                            transcript_files=files)
        self.assertIn("| # | Stage / Target | Model | Duration | Tokens "
                      "| Verdict | Transcript |", text)
        self.assertIn("[001-spec_author.md](sessions/001-spec_author.md)", text)
        self.assertIn("[004-slice_implement-slice-1.md]"
                      "(sessions/004-slice_implement-slice-1.md)", text)
        body = [ln for ln in text.splitlines() if ln.startswith("| ")
                and not ln.startswith("| # ") and not ln.startswith("| ---")]
        self.assertEqual(len(body), 4)
        self.assertEqual(body[2].rstrip().rsplit("|", 2)[-2].strip(), "—")

    def test_slice_and_iteration_are_visible_in_the_target_column(self):
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test")
        self.assertIn("slice 1: implement", text)
        self.assertIn("spec_author (iter 2)", text)

    def test_pipe_in_a_name_cannot_break_the_table(self):
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test")
        self.assertIn("assessor\\|model", text)
        row = [ln for ln in text.splitlines() if "assessor" in ln][0]
        self.assertEqual(row.count("|") - row.count("\\|"), 8)

    def test_diagnostics_sections_are_markdown_lists(self):
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test")
        self.assertIn("### Loops & retries (1 detected)", text)
        self.assertIn("### Blockages & bounces (1 detected)", text)
        self.assertIn("### Time hotspots", text)
        self.assertIn("### Stage summary for 001-test", text)
        self.assertIn("- Step #3 [spec_author]: iteration 2", text)
        self.assertIn("KICKBACK", text)
        self.assertIn("- **slice_implement**: 1 session", text)

    def test_clean_pass_diagnostics_say_so(self):
        rows = [_journey_rows()[0]]
        text = render_task_journey_markdown(rows, task_id="001-test")
        self.assertIn("- Clean straight pass (no retry loops)", text)
        self.assertIn("- No rejections or blockages encountered", text)

    def test_empty_rows_render_a_header_not_a_crash(self):
        text = render_task_journey_markdown([], task_id="empty-task")
        self.assertIn("# Journey: empty-task", text)
        self.assertIn("No sessions recorded", text)


class MatchingTest(unittest.TestCase):
    def test_rows_pair_with_their_sequence_numbers(self):
        transcripts = list_transcripts(_task_dir_with([
            _transcript(1, "spec_author"),
            _transcript(2, "spec_assess_tw"),
            _transcript(3, "spec_author", iteration=2),
            _transcript(4, "slice_implement", slice_id="1"),
        ]))
        self.assertEqual(
            match_rows_to_transcripts(_journey_rows(), transcripts),
            ["001-spec_author.md", "002-spec_assess_tw.md",
             "003-spec_author-iter-2.md", "004-slice_implement-slice-1.md"])

    def test_resumed_numbering_still_links_by_identity(self):
        # A resumed task's transcripts start at 005 while its rows still begin
        # at index 1: sequence alone cannot pair them, stage/slice/iteration can.
        transcripts = list_transcripts(_task_dir_with([
            _transcript(5, "spec_author"),
            _transcript(6, "spec_assess_tw"),
            _transcript(7, "spec_author", iteration=2),
            _transcript(8, "slice_implement", slice_id="1"),
        ]))
        self.assertEqual(
            match_rows_to_transcripts(_journey_rows(), transcripts),
            ["005-spec_author.md", "006-spec_assess_tw.md",
             "007-spec_author-iter-2.md", "008-slice_implement-slice-1.md"])

    def test_missing_and_unparseable_files_yield_none(self):
        task_dir = _task_dir_with([
            _transcript(1, "spec_author"),
            _transcript(2, "spec_assess_tw"),
        ])
        (task_dir / "artifacts" / "sessions" / "notes.md").write_text("stray\n")
        (task_dir / "artifacts" / "sessions" / "0xx-spec_author.md").write_text("stray\n")
        paired = match_rows_to_transcripts(_journey_rows(), list_transcripts(task_dir))
        self.assertEqual(paired[:2], ["001-spec_author.md", "002-spec_assess_tw.md"])
        self.assertEqual(paired[2:], [None, None])

    def test_each_transcript_is_used_at_most_once(self):
        transcripts = list_transcripts(_task_dir_with(
            [_transcript(1, "spec_author")]))
        rows = [_journey_rows()[0], _journey_rows()[0]]
        self.assertEqual(match_rows_to_transcripts(rows, transcripts),
                         ["001-spec_author.md", None])


def _task_dir_with(paths: list[Path]) -> Path:
    """A fake task dir whose `artifacts/sessions/` holds exactly `paths`."""
    task_dir = Path(tempfile.mkdtemp()) / "001-test"
    sessions = task_dir / "artifacts" / "sessions"
    sessions.mkdir(parents=True)
    for path in paths:
        (sessions / path.name).write_text("# transcript\n")
    return task_dir


class FakePiPassTest(unittest.TestCase):
    """`_persist_journey_readout` after real (fake-`pi`) sessions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.task_id = "t1"
        self.task_dir = self.work_dir / "queue" / "active" / self.task_id
        (self.task_dir / "artifacts" / "sessions").mkdir(parents=True)
        self.art_dir = self.task_dir / "artifacts"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        _write_fake_pi(self.bin_dir)
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
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)

    def _run_sessions(self) -> None:
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR)
        self.runner.run("m", self.work_repo, "Implement slice 1.",
                        task_id=self.task_id, stage=Stage.SLICE_IMPLEMENT,
                        slice_id="1")

    def test_journey_md_links_resolve_to_real_transcripts(self):
        self._run_sessions()
        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

        journey_md = self.art_dir / "journey.md"
        self.assertTrue(journey_md.is_file(), f"journey.md missing: {self.lines}")
        text = journey_md.read_text()
        self.assertIn("# Journey: t1", text)

        links = _markdown_links(text)
        self.assertEqual(len(links), 2, f"expected one link per session: {text}")
        for link in links:
            self.assertFalse(link.startswith("/"),
                             f"link must be relative to artifacts/: {link}")
            self.assertTrue((self.art_dir / link).is_file(),
                            f"link does not resolve: {link}")

    def test_journey_txt_stays_byte_identical_to_the_ascii_renderer(self):
        self._run_sessions()
        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)
        rows = self.store.for_task(self.task_id)
        expected = render_task_journey(rows, task_id=self.task_id)
        self.assertEqual((self.art_dir / "journey.txt").read_text(), expected)
        stats_journey = self.cfg.stats_path.parent / "journeys" / f"{self.task_id}-journey.txt"
        self.assertEqual(stats_journey.read_text(), expected)

    def test_session_without_a_transcript_shows_an_em_dash(self):
        self._run_sessions()
        (self.art_dir / "sessions" / "001-spec_author.md").unlink()
        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)
        text = (self.art_dir / "journey.md").read_text()
        rows = [ln for ln in text.splitlines() if ln.startswith("| ")
                and not ln.startswith("| # ") and not ln.startswith("| ---")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].rstrip().rsplit("|", 2)[-2].strip(), "—")
        self.assertIn("002-slice_implement-slice-1.md", text)


    def test_parked_pass_lands_journey_md_with_the_moved_task(self):
        """A park moves the task dir before the `finally` hook runs, so the
        journey must follow it — its links are relative to the `artifacts/`
        directory it sits in."""
        self._run_sessions()
        parked_dir = self.work_dir / "queue" / "parked" / self.task_id
        parked_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.task_dir), str(parked_dir))

        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

        journey_md = parked_dir / "artifacts" / "journey.md"
        self.assertTrue(journey_md.is_file(),
                        f"journey.md did not follow the task: {self.lines}")
        self.assertFalse(self.task_dir.exists(),
                         "the stale active/ path must not be recreated")
        links = _markdown_links(journey_md.read_text())
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertTrue((journey_md.parent / link).is_file(),
                            f"link does not resolve from journey.md: {link}")

    def test_task_with_no_queue_dir_is_skipped_without_a_wreck(self):
        """Rows without a task directory (direct runner use): no file, no raise."""
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id="nowhere", stage=Stage.SPEC_AUTHOR)
        self.pipeline._persist_journey_readout("nowhere",
                                               self.work_dir / "queue" / "active" / "nowhere")
        self.assertFalse((self.art_dir / "journey.md").exists())


def _markdown_links(text: str) -> list[str]:
    """Every `(target)` in the document's Markdown links."""
    import re
    return re.findall(r"\]\(([^)]+)\)", text)


def _write_fake_pi(bin_dir: Path) -> None:
    """An executable `pi` that answers with one canned `message_end` event."""
    body = textwrap.dedent(f"""
        import json
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


if __name__ == "__main__":
    unittest.main()
