"""T30: stage names are `Stage` members at every call site (finding F9).

`pipeline.py` and `autonomous.py` used to pass raw stage strings, two of them
built as `f"{kind}_review"`. A typo in a literal or in that f-string is
invisible to grep and lands verbatim in `sessions.jsonl`, whose historical
rows the stats report re-renders — a changed value is a silent history rewrite.

These tests pin the wire contract:
- no `stage="..."` literal and no f-string-built stage survives in the two
  workflow modules;
- a real `SessionRunner.run` records the byte-identical stage string
  (`"slice_implement"`) in the stats row, whether it is handed the `Stage`
  member or a stray string (the edge conversion is tolerant, never raising).

Run from the repo root:  python3 -m unittest tests.test_stage_at_callsites
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import ReviewKind, Stage, Verdict
from harness.core.session import SessionResult, SessionRunner
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline
from harness.core.stats import StatsStore
from external.pi_cli import PiSessionResult

WORKFLOW_MODULES = (
    Path("harness/workflow/pipeline.py"),
    Path("harness/workflow/autonomous.py"),
)


def _cfg(work_dir: Path) -> Config:
    return Config(
        work_dir=work_dir,
        token_budget=100_000,
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
        model_context_map={},
    )


def _fake_pi(output: str):
    """Stand in for `external.pi_cli.run_pi_session`: no subprocess, fixed text."""

    def run(*, model, workdir, prompt, out_file, log) -> PiSessionResult:
        Path(out_file).write_text(output)
        return PiSessionResult(rc=0, crashed=False, err="", peak_tokens=7,
                               duration_s=0.1, output=output,
                               out_file=Path(out_file), stderr="")

    return run


class StageCallSiteSourceTest(unittest.TestCase):
    """The source itself: no raw literals, no f-string-built stages."""

    def test_no_raw_stage_literals_in_workflow_modules(self):
        for rel in WORKFLOW_MODULES:
            src = (Path(__file__).resolve().parent.parent / rel).read_text()
            bad = re.findall(r'stage\s*=\s*["\'][a-z_]+["\']', src)
            self.assertEqual(bad, [], f"{rel}: raw stage literals {bad}")
            self.assertNotIn("_review}", src,
                             f"{rel}: f-string-built stage still present")

    def test_stage_members_cover_every_produced_value(self):
        vals = {m.value for m in Stage}
        need = {"spec_author", "spec_assess_ornith", "spec_assess_tw",
                "feasibility", "slicing", "slice_check", "slice_implement",
                "tech_review", "func_review", "slice_fix", "holistic",
                "autonomous_suggest", "autonomous_review"}
        self.assertEqual(sorted(need - vals), [])


class StageWireStringTest(unittest.TestCase):
    """`SessionRunner.run` writes the enum's value, byte-identical to history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.cfg = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        self.runner = SessionRunner(self.cfg, self.store, log=lambda *a: None)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self._pi = patch("harness.core.session.run_pi_session",
                         _fake_pi("## Summary\nworked\n\nVERDICT: done"))
        self._pi.start()
        self.addCleanup(self._pi.stop)

    def _row(self) -> dict:
        """The last recorded row (the store is append-only across sub-tests)."""
        rows = self.store.all()
        self.assertTrue(rows, "no stats row recorded")
        return rows[-1]

    def test_slice_implement_wire_string_is_unchanged(self):
        self.runner.run("m", self.work_repo, "p", task_id="t1",
                        stage=Stage.SLICE_IMPLEMENT, slice_id="1.1")
        self.assertEqual(self._row()["stage"], "slice_implement")

    def test_every_stage_member_records_its_value(self):
        for member in Stage:
            with self.subTest(stage=member.value):
                self.runner.run("m", self.work_repo, "p", task_id="t1",
                                stage=member)
                self.assertEqual(self._row()["stage"], member.value)

    def test_stray_string_stage_is_recorded_not_raised(self):
        self.runner.run("m", self.work_repo, "p", task_id="t1", stage="smoke")
        self.assertEqual(self._row()["stage"], "smoke")

    def test_default_stage_is_recorded(self):
        self.runner.run("m", self.work_repo, "p", task_id="t1")
        self.assertEqual(self._row()["stage"], "unknown")


class RecordingRunner:
    """Stands in for `SessionRunner`: records the `stage=` it was handed."""

    def __init__(self):
        self.stages: list = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        self.stages.append(stage)
        out_file = Path(workdir) / f".pi-session-{stage}-{len(self.stages)}.out"
        out_file.write_text("VERDICT: done")
        return SessionResult(ok=True, verdict="done", peak_tokens=0,
                             duration_s=0.0, output="VERDICT: done",
                             out_file=out_file, crashed=False)


class PipelineCallSiteTest(unittest.TestCase):
    """`Pipeline._run` receives `Stage` members, never literals or f-strings."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.cfg = _cfg(self.work_dir)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.runner = RecordingRunner()
        self.pipeline = Pipeline(self.cfg, self.runner, log=lambda *a: None)

    def test_review_loop_passes_the_review_stage_members(self):
        ctx = StageContext("t1", self.work_dir, self.work_repo)
        with patch.object(self.pipeline.lifecycle, "park",
                          lambda *a, **k: None):
            self.pipeline._review_loop(ctx, "1.1", ReviewKind.TECH,
                                       Stage.TECH_REVIEW)
            self.pipeline._review_loop(ctx, "1.1", ReviewKind.FUNC,
                                       Stage.FUNC_REVIEW)
        self.assertEqual(self.runner.stages,
                         [Stage.TECH_REVIEW, Stage.FUNC_REVIEW])

    def test_fix_session_is_staged_as_slice_fix(self):
        """A failing review's fix run is `Stage.SLICE_FIX` for both kinds."""
        # Script: review fails, the fix session runs, the re-review passes.
        script = [Verdict.FAIL, Verdict.DONE, Verdict.PASS]

        def scripted(model, workdir, prompt, **kw):
            stage = kw.get("stage")
            self.runner.stages.append(stage)
            verdict = script.pop(0)
            out_file = Path(workdir) / f".pi-session-{stage}.out"
            out_file.write_text("VERDICT: " + verdict.value)
            return SessionResult(ok=True, verdict=verdict, peak_tokens=0,
                                 duration_s=0.0,
                                 output="VERDICT: " + verdict.value,
                                 out_file=out_file, crashed=False)

        self.runner.run = scripted
        ctx = StageContext("t1", self.work_dir, self.work_repo)
        (ctx.task_dir / "artifacts" / "progress").mkdir(parents=True)
        with patch.object(self.pipeline.lifecycle, "park",
                          lambda *a, **k: None):
            self.pipeline._review_loop(ctx, "1.1", ReviewKind.FUNC,
                                       Stage.FUNC_REVIEW)
        self.assertEqual(self.runner.stages,
                         [Stage.FUNC_REVIEW, Stage.SLICE_FIX,
                          Stage.FUNC_REVIEW])


if __name__ == "__main__":
    unittest.main()
