"""Slice 5 (001-full-interactions-logged): edge-case hardening sweep.

Targeted regression pins for the spec edge cases the happy-path suites leave
open (AC 2, 4, 5, 7, 8; FR-1/2/3):

- encoding: text that survived the stream decode but cannot be encoded (a
  lone surrogate) is written with U+FFFD replacement — no UnicodeEncodeError
  escapes a transcript write (task pool or pooled pool), and one unencodable
  stats row cannot cost the operator `journey.md`;
- FR-3 exact warning: an unwritable artifacts dir produces
  `transcript write failed for session NNN-<stage>: <exc>` and the run
  continues with its stats row intact;
- FR-2/AC 7 numbering: `next_sequence` is `max(stats rows, on-disk) + 1` in
  both directions, and a resume past restored transcripts never rewrites them;
- FR-4/FR-8 renderer: pipes and newlines inside Sessions-table cells cannot
  break rows; non-ASCII (emoji, arrows) passes through Mermaid labels
  untouched while structural characters stay escaped;
- FR-4: `journey.md` lands beside a task that moved to `failed/` during the
  pass, with a parseable `flowchart LR` block and resolving links;
- AC 8: the stats row schema (`sessions.jsonl`) is unchanged.

Driven by fake `pi` scripts on `PATH` (never the real binary — `setUp`
asserts `shutil.which` resolves inside the temp dir), in temp dirs only.

Run from the repo root:  python3 -m unittest tests.test_edge_case_hardening
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import (
    SessionRecord,
    StatsStore,
    render_task_journey_markdown,
)
from harness.core.transcripts import (
    TranscriptRecord,
    next_sequence,
    sessions_dir_for,
    write_pooled_transcript,
    write_transcript,
)
from harness.workflow.pipeline import Pipeline

from tests.test_transcript_basic import _cfg
from tests.test_transcript_edge_cases import _fake_pi, _message_event

LONE_SURROGATE = "dead\ud800end"


def _record(**overrides) -> TranscriptRecord:
    """One valid transcript record; tests override only what they pin."""
    fields = dict(
        sequence=1, task_id="t1", stage="spec_author", timestamp="now",
        model="m", duration_s=1.0, peak_tokens=5, rc=0, verdict="pass",
        crashed=False, prompt="p", output="o", stderr="",
    )
    fields.update(overrides)
    return TranscriptRecord(**fields)


class TranscriptEncodingTest(unittest.TestCase):
    """§8 encoding: undecodable-at-write text becomes U+FFFD, never a raise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.lines: list[str] = []

    def test_task_pool_writes_lone_surrogates_with_replacement(self):
        task_dir = self.root / "queue" / "active" / "t1"
        path = write_transcript(
            task_dir,
            _record(prompt=LONE_SURROGATE, output=LONE_SURROGATE,
                    stderr=LONE_SURROGATE),
            self.lines.append,
        )
        self.assertIsNotNone(path, f"write must succeed: {self.lines}")
        self.assertEqual(self.lines, [])
        text = path.read_text(encoding="utf-8")
        # `errors="replace"` substitutes the unencodable surrogate; the raw
        # codepoint must not survive and the surrounding text must.
        self.assertNotIn("\ud800", text)
        self.assertEqual(len(re.findall(r"dead.end", text)), 3)

    def test_pooled_pool_writes_lone_surrogates_with_replacement(self):
        path = write_pooled_transcript(
            self.root, _record(sequence=None, task_id=None,
                               output=LONE_SURROGATE),
            self.lines.append,
        )
        self.assertIsNotNone(path, f"pooled write must succeed: {self.lines}")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("\ud800", text)
        self.assertRegex(text, r"dead.end")


class NextSequenceTest(unittest.TestCase):
    """FR-2: `max(stats rows, transcripts on disk) + 1`, never overwriting."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = Path(self._tmp.name) / "queue" / "active" / "t1"
        self.sessions = sessions_dir_for(self.task_dir)
        self.sessions.mkdir(parents=True)
        self.lines: list[str] = []

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.sessions / name).write_text("# restored\n")

    def test_stats_rows_win_when_more(self):
        self._seed("001-spec_author.md")
        self.assertEqual(next_sequence(3, self.task_dir), 4)

    def test_on_disk_transcripts_win_when_more(self):
        self._seed("001-spec_author.md", "002-spec_assess.md",
                   "003-slice_implement-slice-1.md")
        self.assertEqual(next_sequence(1, self.task_dir), 4)

    def test_resume_never_rewrites_restored_transcripts(self):
        self._seed("001-spec_author.md", "002-spec_assess.md")
        original = (self.sessions / "001-spec_author.md").read_text()
        path = write_transcript(self.task_dir,
                                _record(sequence=next_sequence(2, self.task_dir)),
                                self.lines.append)
        self.assertEqual(path.name, "003-spec_author.md")
        self.assertEqual((self.sessions / "001-spec_author.md").read_text(),
                         original)
        self.assertEqual((self.sessions / "002-spec_assess.md").read_text(),
                         "# restored\n")


class FakePiCase(unittest.TestCase):
    """Shared fake-`pi` harness (temp queue/stats dirs, PATH-isolated `pi`)."""

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
        _fake_pi(self.bin_dir, _message_event("All good.\n\nVERDICT: done"))

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


class Fr3WarningTest(FakePiCase):
    """AC 5 / FR-3: exact warning text, pipeline unaffected."""

    def test_unwritable_dir_emits_the_exact_fr3_warning_and_continues(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: directory permissions are not enforced")
        sessions = self.art_dir / "sessions"
        sessions.chmod(0o500)
        self.addCleanup(sessions.chmod, 0o700)

        result = self.runner.run("m", self.work_repo, "Do the thing.",
                                 task_id=self.task_id, stage=Stage.SPEC_AUTHOR)

        self.assertTrue(result.ok, f"session must still succeed: {self.lines}")
        warning = next((ln for ln in self.lines
                        if "transcript write failed" in ln), None)
        self.assertIsNotNone(warning, f"no FR-3 warning in: {self.lines}")
        self.assertRegex(
            warning,
            r"transcript write failed for session 001-spec_author: .+")
        self.assertEqual(len(self.store.for_task(self.task_id)), 1)


class JourneyEncodingTest(FakePiCase):
    """One unencodable stats row must not cost the operator `journey.md`."""

    def test_journey_md_written_when_a_row_holds_a_lone_surrogate(self):
        self.store.record(SessionRecord(
            ts="2026-01-01T00:00:00Z", task_id=self.task_id,
            stage="spec_author", model=f"model\ud800", verdict="done",
            outcome="done", peak_tokens=10, duration_s=1.0, rc=0,
            notes=f"retry\ud800",
        ))
        (self.art_dir / "sessions" / "001-spec_author.md").write_text(
            "# transcript\n")

        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

        journey_md = self.art_dir / "journey.md"
        self.assertTrue(journey_md.is_file(),
                        f"journey.md must survive the bad row: {self.lines}")
        text = journey_md.read_text(encoding="utf-8")
        self.assertIn("```mermaid", text)
        self.assertIn("flowchart LR", text)
        self.assertIn("[001-spec_author.md](sessions/001-spec_author.md)",
                      text)


class JourneyTableEscapingTest(unittest.TestCase):
    """FR-8: pipes and newlines inside cells cannot break the table rows."""

    def _rows(self) -> list[dict]:
        return [
            {"ts": "t", "task_id": "t1", "stage": "spec|author",
             "model": "mo\ndel", "verdict": "pass|done", "outcome": "pass",
             "peak_tokens": 9, "duration_s": 2.0, "rc": 0, "iteration": 1},
            {"ts": "t", "task_id": "t1", "stage": "slice_implement",
             "model": "m", "verdict": "done", "outcome": "done",
             "peak_tokens": 8, "duration_s": 1.0, "rc": 0, "slice": "1",
             "iteration": 1},
        ]

    def test_rows_stay_one_line_with_seven_cells(self):
        text = render_task_journey_markdown(self._rows(), task_id="t1",
                                            transcript_files=["001-a.md", None])
        body_rows = [ln for ln in text.splitlines()
                     if ln.startswith("| ") and not ln.startswith("| #")
                     and not ln.startswith("| ---")]
        self.assertEqual(len(body_rows), 2, text)
        for row in body_rows:
            self.assertNotIn("\n", row)
            cells = re.split(r"(?<!\\)\|", row)
            self.assertEqual(len(cells), 9, f"row cells broken: {row!r}")
        self.assertIn("spec\\|author", text)
        self.assertIn("pass\\|done", text)


class MermaidLabelPassthroughTest(unittest.TestCase):
    """FR-8: non-ASCII passes through labels; structural characters stay escaped."""

    def test_emoji_and_arrows_survive_labels_untouched(self):
        rows = [{"ts": "t", "task_id": "t1",
                 "stage": "spec_author🚀", "model": "m",
                 "verdict": "pass→ok", "outcome": "pass",
                 "peak_tokens": 9, "duration_s": 1.0, "rc": 0,
                 "iteration": 1}]
        text = render_task_journey_markdown(rows, task_id="t1")
        block = text.split("```mermaid", 1)[1].split("```", 1)[0]
        self.assertIn("spec_author🚀", block)
        self.assertIn("pass→ok", block)

    def test_structural_characters_stay_escaped_next_to_non_ascii(self):
        rows = [{"ts": "t", "task_id": "t1",
                 "stage": "stage🚀\"[]|`#", "model": "m",
                 "verdict": "done", "outcome": "done",
                 "peak_tokens": 1, "duration_s": 1.0, "rc": 0,
                 "iteration": 1}]
        text = render_task_journey_markdown(rows, task_id="t1")
        block = text.split("```mermaid", 1)[1].split("```", 1)[0]
        node = next(ln for ln in block.splitlines() if "N1[" in ln)
        self.assertIn("stage🚀", node)
        for ch in ('"', "[", "]", "|", "`"):
            self.assertNotIn(ch, node.replace('N1["', "").rstrip('"]'),
                             f"raw {ch!r} survived in {node!r}")


class FailedPassJourneyTest(FakePiCase):
    """FR-4: a pass that failed and moved the task still lands `journey.md`."""

    def test_journey_md_follows_the_task_into_failed(self):
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR)
        failed_dir = self.work_dir / "queue" / "failed" / self.task_id
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.task_dir), str(failed_dir))

        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

        journey_md = failed_dir / "artifacts" / "journey.md"
        self.assertTrue(journey_md.is_file(),
                        f"journey.md did not follow the task: {self.lines}")
        self.assertFalse(self.art_dir.exists(),
                         "the stale active/ path must not be recreated")
        text = journey_md.read_text(encoding="utf-8")
        self.assertIn("flowchart LR", text)
        for link in re.findall(r"\]\(([^)]+)\)", text):
            self.assertTrue((journey_md.parent / link).is_file(),
                            f"link does not resolve: {link}")


class StatsSchemaTest(FakePiCase):
    """AC 8: `sessions.jsonl` rows keep their exact schema."""

    def test_row_keys_are_unchanged(self):
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR)
        row = json.loads(self.cfg.stats_path.read_text().splitlines()[-1])
        self.assertEqual(
            set(row),
            {"ts", "task_id", "stage", "model", "verdict", "outcome",
             "peak_tokens", "duration_s", "rc", "prompt_chars", "slice",
             "iteration", "session_file", "notes"})


if __name__ == "__main__":
    unittest.main()
