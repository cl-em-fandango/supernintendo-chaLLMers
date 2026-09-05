"""T55: a review fix session always runs on the implementer model.

`Pipeline._review_loop` picked one model per review kind and used it for both
the review and the fix that follows a failing review:

    model = self.cfg.implementer if kind is ReviewKind.TECH else self.cfg.model

So a *functional*-review fix — a code edit — was handed to the technical
writer. The model choice followed the review type instead of the work type.
Reviewer selection is correct and stays as it is; both fix paths now take
`self.cfg.implementer`.

These tests pin the exact model on every call `_review_loop` makes, per kind:
reviewer calls keep their per-kind model, fix calls are the implementer.

Run from the repo root:  python3 -m unittest tests.test_review_fix_model
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import ReviewKind, Stage, Verdict
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline

# Distinct names per role: an assertion on a model string is only meaningful
# when the roles cannot be satisfied by the same value.
IMPLEMENTER = "impl-model"
WRITER = "writer-model"
ASSESSOR = "assessor-model"


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

    Records the `(model, stage)` pair of every call in order, so a test can
    assert the whole call sequence of one `_review_loop` pass rather than the
    fix call in isolation.
    """

    def __init__(self, verdicts: list[Verdict]):
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, Stage]] = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        self.calls.append((model, stage))
        verdict = self.verdicts.pop(0)
        out_file = Path(workdir) / f".pi-session-{stage}-{len(self.calls)}.out"
        out_file.write_text("## Summary\nscripted\n\nVERDICT: " + verdict.value)
        return SessionResult(ok=True, verdict=verdict, peak_tokens=0,
                             duration_s=0.0,
                             output="## Summary\nscripted\n\nVERDICT: " + verdict.value,
                             out_file=out_file, crashed=False)


class ReviewFixModelTest(unittest.TestCase):
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

    def _run_loop(self, kind: ReviewKind, stage: Stage,
                  verdicts: list[Verdict]) -> ScriptedRunner:
        """Drive one `_review_loop` pass and hand back its recorded calls."""
        runner = ScriptedRunner(verdicts)
        pipeline = Pipeline(self.cfg, runner, log=lambda *a: None)
        ctx = StageContext("t1", self.task_dir, self.work_repo)
        with patch.object(pipeline.lifecycle, "park",
                          lambda _tid, reason: self.parked.append(reason)):
            passed = pipeline._review_loop(ctx, "1.1", kind, stage)
        self.assertTrue(passed, f"loop did not pass: {self.parked}")
        self.assertEqual(self.parked, [])
        self.assertEqual(runner.verdicts, [],
                         "scripted verdicts left over: the loop made fewer calls "
                         f"than scripted — calls={runner.calls}")
        return runner

    def test_tech_review_and_fix_both_run_on_the_implementer(self):
        """Review fails, the fix runs, the re-review passes."""
        runner = self._run_loop(
            ReviewKind.TECH, Stage.TECH_REVIEW,
            [Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        self.assertEqual(runner.calls, [
            (IMPLEMENTER, Stage.TECH_REVIEW),
            (IMPLEMENTER, Stage.SLICE_FIX),
            (IMPLEMENTER, Stage.TECH_REVIEW),
        ])

    def test_func_review_fix_runs_on_the_implementer(self):
        """The functional reviewer stays the technical writer; its fix does not."""
        runner = self._run_loop(
            ReviewKind.FUNC, Stage.FUNC_REVIEW,
            [Verdict.FAIL, Verdict.DONE, Verdict.PASS])
        self.assertEqual(runner.calls, [
            (WRITER, Stage.FUNC_REVIEW),
            (IMPLEMENTER, Stage.SLICE_FIX),
            (WRITER, Stage.FUNC_REVIEW),
        ])

    def test_every_fix_in_a_multi_iteration_loop_uses_the_implementer(self):
        """Two failing functional reviews, two fixes, both on the implementer."""
        runner = self._run_loop(
            ReviewKind.FUNC, Stage.FUNC_REVIEW,
            [Verdict.FAIL, Verdict.DONE, Verdict.FAIL, Verdict.DONE,
             Verdict.PASS])
        self.assertEqual(runner.calls, [
            (WRITER, Stage.FUNC_REVIEW),
            (IMPLEMENTER, Stage.SLICE_FIX),
            (WRITER, Stage.FUNC_REVIEW),
            (IMPLEMENTER, Stage.SLICE_FIX),
            (WRITER, Stage.FUNC_REVIEW),
        ])

    def test_reviewer_selection_is_unchanged(self):
        """A passing review is one call, on the per-kind reviewer model."""
        tech = self._run_loop(ReviewKind.TECH, Stage.TECH_REVIEW,
                              [Verdict.PASS])
        self.assertEqual(tech.calls, [(IMPLEMENTER, Stage.TECH_REVIEW)])
        func = self._run_loop(ReviewKind.FUNC, Stage.FUNC_REVIEW,
                              [Verdict.PASS])
        self.assertEqual(func.calls, [(WRITER, Stage.FUNC_REVIEW)])


if __name__ == "__main__":
    unittest.main()
