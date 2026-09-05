"""T56: review feedback and implementation progress are separate files.

`Pipeline._implement` writes an unfinished slice's progress note to
`artifacts/progress/slice-<id>.md`, and `Pipeline._review_loop` wrote a failing
review's feedback to that *same* path. Two artifacts, two readers — the next
implement session reads the progress note, the fix session reads the review
feedback — one file: a review report could be read back as a progress note, and
`_implement`'s "never overwrite an existing note" guard made whichever note came
first stick for the rest of the task.

Review feedback now lands on `artifacts/progress/slice-<id>-review.md`; the
implementation progress path is unchanged. These tests pin both paths, pin that
both files survive a progress-then-review cycle with their own content, and pin
that the fix session is told to open the review file.

Run from the repo root:  python3 -m unittest tests.test_review_note_path
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import ReviewKind, Stage, Verdict
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline

IMPLEMENTER = "impl-model"
WRITER = "writer-model"
ASSESSOR = "assessor-model"
SLICE = "1.1"


def _cfg(work_dir: Path) -> Config:
    return Config(
        harness_execution_and_queue_dir=work_dir,
        token_budget=100_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=3,
        max_slice_func_review=3,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": WRITER,
                "implementer": IMPLEMENTER,
                "assessor": ASSESSOR},
        model_context_map={},
    )


class ScriptedRunner:
    """Stands in for `SessionRunner`: no subprocess, scripted verdicts.

    Each call writes a distinct `.out` file — "session N (stage)" — so a copied
    artifact can be traced back to the session it came from, and records the
    prompt so a test can read which note file the session was told to open.
    """

    def __init__(self, verdicts: list[Verdict]):
        self.verdicts = list(verdicts)
        self.prompts: list[str] = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        self.prompts.append(prompt)
        verdict = self.verdicts.pop(0)
        body = f"## Summary\nsession {len(self.prompts)} ({stage.value})\n\nVERDICT: " + verdict.value
        out_file = Path(workdir) / f".pi-session-{len(self.prompts)}.out"
        out_file.write_text(body)
        return SessionResult(ok=True, verdict=verdict, peak_tokens=0,
                             duration_s=0.0, output=body, out_file=out_file,
                             crashed=False)


class ReviewNotePathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.cfg = _cfg(self.work_dir)
        self.task_dir = self.work_dir / "active" / "t1"
        (self.task_dir / "artifacts" / "progress").mkdir(parents=True)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.parked: list[str] = []
        self.runner = None
        self.pipeline = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def progress_note(self) -> Path:
        """The implementation progress path `_implement` has always used."""
        return self.task_dir / "artifacts" / "progress" / f"slice-{SLICE}.md"

    def review_note(self) -> Path:
        """The review-feedback path T56 introduces."""
        return self.task_dir / "artifacts" / "progress" / f"slice-{SLICE}-review.md"

    def _drive(self, verdicts: list[Verdict]) -> ScriptedRunner:
        """Build one scripted pipeline; the caller runs its stages."""
        self.runner = ScriptedRunner(verdicts)
        self.pipeline = Pipeline(self.cfg, self.runner, log=lambda *a: None)
        self.pipeline.lifecycle.park = lambda _tid, reason: self.parked.append(reason)
        return self.runner

    def _ctx(self) -> StageContext:
        return StageContext("t1", self.task_dir, self.work_repo)

    def _implement(self, verdicts: list[Verdict]) -> ScriptedRunner:
        runner = self._drive(verdicts)
        self.assertTrue(self.pipeline._implement(self._ctx(), SLICE),
                        f"_implement did not deliver: {self.parked}")
        return runner

    def _review(self, verdicts: list[Verdict]) -> ScriptedRunner:
        runner = self._drive(verdicts)
        self.assertTrue(
            self.pipeline._review_loop(self._ctx(), SLICE, ReviewKind.TECH,
                                       Stage.TECH_REVIEW),
            f"review loop did not pass: {self.parked}")
        return runner

    # ------------------------------------------------------------------
    # review feedback path
    # ------------------------------------------------------------------
    def test_review_feedback_is_written_to_the_review_path(self):
        """A failing review's report lands on `slice-<id>-review.md`."""
        self._implement([Verdict.DONE])
        self._review([Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        self.assertTrue(self.review_note().exists(),
                        "review feedback was not written to the review path")
        self.assertIn(f"({Stage.TECH_REVIEW.value})",
                      self.review_note().read_text())

    def test_review_feedback_does_not_touch_the_progress_path(self):
        """With no progress note in existence, a review creates only its own file."""
        self._implement([Verdict.DONE])
        self._review([Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        self.assertFalse(self.progress_note().exists(),
                         "the review loop still writes the implementation "
                         "progress path")

    def test_fix_session_is_told_to_read_the_review_path(self):
        """The fix prompt points at the review file, not at the progress note."""
        self._implement([Verdict.DONE])
        runner = self._review([Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        fix_prompt = runner.prompts[1]
        self.assertIn(f"slice-{SLICE}-review.md", fix_prompt)
        self.assertNotIn(f"slice-{SLICE}.md", fix_prompt)

    # ------------------------------------------------------------------
    # implementation progress path is preserved
    # ------------------------------------------------------------------
    def test_progress_note_keeps_its_path(self):
        """`_implement` still writes `slice-<id>.md` and nothing else."""
        self._implement([Verdict.PROGRESS, Verdict.DONE])
        self.assertTrue(self.progress_note().exists(),
                        "the implementation progress note moved off its path")
        self.assertIn(f"({Stage.SLICE_IMPLEMENT.value})",
                      self.progress_note().read_text())
        self.assertFalse(self.review_note().exists())

    def test_a_review_note_does_not_suppress_a_later_progress_note(self):
        """The two paths no longer collide: a review note cannot block a
        progress note, because `_implement`'s keep-the-first-note guard only
        sees the progress path."""
        self._implement([Verdict.DONE])
        self._review([Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        self.assertTrue(self.review_note().exists())
        self._implement([Verdict.PROGRESS, Verdict.DONE])
        self.assertTrue(self.progress_note().exists(),
                        "an existing review note blocked the progress note")

    # ------------------------------------------------------------------
    # both files survive
    # ------------------------------------------------------------------
    def test_both_files_survive_a_progress_then_review_cycle(self):
        """One slice, one runner: PROGRESS note, delivery, failing review, fix,
        pass. Both notes exist afterwards, each holding its own session's text."""
        runner = self._drive([Verdict.PROGRESS, Verdict.DONE,
                              Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        ctx = self._ctx()
        self.assertTrue(self.pipeline._implement(ctx, SLICE), str(self.parked))
        self.assertTrue(
            self.pipeline._review_loop(ctx, SLICE, ReviewKind.TECH,
                                       Stage.TECH_REVIEW),
            str(self.parked))
        self.assertEqual(runner.verdicts, [],
                         "scripted verdicts left over: fewer calls than scripted")
        self.assertEqual(self.parked, [])

        self.assertTrue(self.progress_note().exists())
        self.assertTrue(self.review_note().exists())
        progress = self.progress_note().read_text()
        review = self.review_note().read_text()
        self.assertIn("session 1 (slice_implement)", progress)
        self.assertIn("session 3 (tech_review)", review)
        self.assertNotIn("tech_review", progress)
        self.assertNotIn("slice_implement", review)

    def test_repeated_reviews_overwrite_only_the_review_note(self):
        """Two failing reviews rewrite the review file; the progress note from
        the unfinished implementation iteration is untouched."""
        runner = self._drive([Verdict.PROGRESS, Verdict.DONE,
                              Verdict.FAIL, Verdict.DONE,
                              Verdict.FAIL, Verdict.DONE,
                              Verdict.PASS])
        ctx = self._ctx()
        self.assertTrue(self.pipeline._implement(ctx, SLICE), str(self.parked))
        self.assertTrue(
            self.pipeline._review_loop(ctx, SLICE, ReviewKind.TECH,
                                       Stage.TECH_REVIEW),
            str(self.parked))
        self.assertEqual(runner.verdicts, [])
        self.assertIn("session 5 (tech_review)", self.review_note().read_text())
        self.assertIn("session 1 (slice_implement)",
                      self.progress_note().read_text())


if __name__ == "__main__":
    unittest.main()
