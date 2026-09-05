"""T43: specification assessment fails closed (hardening review G1/G2/G3).

`stage_spec` used to single out `KICKBACK` and treat *everything else* — an
errored session, a session that said nothing decidable, a verdict the assessor
prompt does not even define — as approval. The protocol is now explicit
(`workflow.spec_assessment.assess_spec`): a healthy `PASS` approves, a
`KICKBACK` revises, anything else parks.

These tests drive `stage_spec` with a stub runner (no subprocess, no stats) and
prove, for both assessors:
- `ERROR`, `NO_VERDICT`, `UNKNOWN`, `FAIL` and every other non-kickback verdict
  cannot reach `spec approved`;
- a session that did not finish cleanly cannot approve even when its partial
  output carries `VERDICT: pass`;
- the kickback loop keeps its shared counter, its artifact naming and its
  maximum (`maxSpecKickbacks`) exactly as before.

Run from the repo root:  python3 -m unittest tests.test_spec_assessment_routing
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage, Verdict
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import AllAttemptsCrashed, Pipeline
from harness.workflow.spec_assessment import SpecAssessment, assess_spec

ASSESSORS = (("ornith", Stage.SPEC_ASSESS_ORNITH), ("tw", Stage.SPEC_ASSESS_TW))

# Every verdict the assessor could come back with that is neither an approval
# nor a revision request — the old code approved all of them.
NOT_AN_APPROVAL = (
    Verdict.ERROR, Verdict.NO_VERDICT, Verdict.UNKNOWN, Verdict.FAIL,
    Verdict.DONE, Verdict.PROGRESS, Verdict.RESLICED, Verdict.INFEASIBLE,
    Verdict.REJECT, Verdict.KICKOUT,
)


def _key(stage) -> str:
    """A `Stage` member's wire value; a stray string passes through (the same
    tolerant conversion `SessionRunner.run` makes at the stats edge)."""
    return stage.value if isinstance(stage, Stage) else str(stage)


def _cfg(work_dir: Path, max_spec_kickbacks: int = 3) -> Config:
    return Config(
        harness_execution_and_queue_dir=work_dir,
        token_budget=100_000,
        max_spec_kickbacks=max_spec_kickbacks,
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


class StubRunner:
    """Stands in for `SessionRunner`: scripted verdict per stage, no subprocess.

    `verdicts` and `sequences` are keyed by stage wire value (or `Stage`
    member); `sequences` wins and is consumed one entry per call, repeating its
    last entry. `crashed` / `bad_rc` name the stages whose session does not
    finish cleanly — the recorded verdict is still whatever was scripted, so a
    stage can be handed `PASS` output from a dead process, which is exactly the
    case the routing must refuse to read as approval.
    """

    DEFAULTS = {
        Stage.SPEC_AUTHOR.value: Verdict.DONE,
        Stage.SPEC_ASSESS_ORNITH.value: Verdict.PASS,
        Stage.SPEC_ASSESS_TW.value: Verdict.PASS,
    }

    def __init__(self, verdicts=None, sequences=None, crashed=(), bad_rc=()):
        self.calls: list[str] = []
        self.results: list[SessionResult] = []
        self.verdicts = {**self.DEFAULTS,
                         **{_key(k): v for k, v in (verdicts or {}).items()}}
        self.sequences = {_key(k): list(v) for k, v in (sequences or {}).items()}
        self._index: dict[str, int] = {}
        self.crashed = {_key(s) for s in crashed}
        self.bad_rc = {_key(s) for s in bad_rc}

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            **kw) -> SessionResult:
        key = _key(stage)
        self.calls.append(key)
        verdict = self._verdict_for(key)
        output = f"## Summary\nassessment text\n\nVERDICT: {verdict.value}"
        out_file = Path(workdir) / f".pi-session-{key}-{len(self.calls)}.out"
        out_file.write_text(output)
        result = SessionResult(ok=key not in self.crashed and key not in self.bad_rc,
                               verdict=verdict, peak_tokens=0, duration_s=0.0,
                               output=output, out_file=out_file,
                               crashed=key in self.crashed)
        self.results.append(result)
        return result

    def _verdict_for(self, key: str) -> Verdict:
        if key not in self.sequences:
            return self.verdicts.get(key, Verdict.PASS)
        seq = self.sequences[key]
        i = self._index.get(key, 0)
        self._index[key] = i + 1
        return seq[i] if i < len(seq) else seq[-1]


class RecordingLifecycle:
    """Stands in for `TaskLifecycle`: records park reasons, moves nothing.

    `stage_spec` only ever calls `park`, so the recorder needs no other method.
    """

    def __init__(self):
        self.parked: list[tuple[str, str]] = []

    def park(self, task_id: str, reason: str) -> None:
        self.parked.append((task_id, reason))


class SpecAssessmentRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.task_dir = self.work_dir / "queue" / "active" / "t1"
        (self.task_dir / "artifacts").mkdir(parents=True)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.ctx = StageContext("t1", self.task_dir, self.work_repo)

    def _spec(self, runner: StubRunner, max_spec_kickbacks: int = 3) -> bool:
        """Run `stage_spec` against `runner`, recording logs and park reasons."""
        self.lines: list[str] = []
        pipeline = Pipeline(_cfg(self.work_dir, max_spec_kickbacks), runner,
                            log=self.lines.append)
        self.lifecycle = RecordingLifecycle()
        pipeline.lifecycle = self.lifecycle
        return pipeline.stage_spec(self.ctx)

    @property
    def _log(self) -> str:
        return "\n".join(self.lines)

    @property
    def _reasons(self) -> list[str]:
        return [reason for _, reason in self.lifecycle.parked]

    # ------------------------------------------------------------------
    # the happy path is unchanged: two healthy PASSes approve
    # ------------------------------------------------------------------
    def test_two_healthy_passes_approve(self):
        runner = StubRunner()
        self.assertTrue(self._spec(runner))
        self.assertEqual(runner.calls, [Stage.SPEC_AUTHOR.value,
                                        Stage.SPEC_ASSESS_ORNITH.value,
                                        Stage.SPEC_ASSESS_TW.value])
        self.assertIn("spec approved", self._log)
        self.assertEqual(self._reasons, [])

    # ------------------------------------------------------------------
    # G1: no other verdict approves, at either assessor
    # ------------------------------------------------------------------
    def test_no_other_verdict_approves_at_either_assessor(self):
        for assessor, stage in ASSESSORS:
            for verdict in NOT_AN_APPROVAL:
                with self.subTest(assessor=assessor, verdict=verdict.value):
                    runner = StubRunner(verdicts={stage: verdict})
                    self.assertFalse(self._spec(runner))
                    self.assertNotIn("spec approved", self._log)
                    self.assertEqual(len(self._reasons), 1,
                                     f"{assessor}/{verdict.value} did not park once")
                    reason = self._reasons[0]
                    self.assertIn(assessor, reason)
                    self.assertIn(verdict.value, reason)

    def test_error_and_no_verdict_specifically_cannot_approve(self):
        """The four verdicts named in the finding, spelled out one by one."""
        for verdict in (Verdict.ERROR, Verdict.NO_VERDICT, Verdict.UNKNOWN,
                        Verdict.FAIL):
            for assessor, stage in ASSESSORS:
                with self.subTest(assessor=assessor, verdict=verdict.value):
                    runner = StubRunner(verdicts={stage: verdict})
                    self.assertFalse(self._spec(runner))
                    self.assertNotIn("spec approved", self._log)

    # ------------------------------------------------------------------
    # G2: a process failure is not a content verdict
    # ------------------------------------------------------------------
    def test_crashed_assessor_with_pass_output_cannot_approve(self):
        """A dead process never approves.

        T57 moved the park for this case out of the stage: every attempt
        crashed, so `_run` raises `AllAttemptsCrashed` and `process` parks —
        the assessor's verdict is never read and `stage_spec` parks nothing
        itself. What is asserted here is the part this card owns: approval is
        unreachable. The park itself is tests/test_all_attempts_crashed.py.
        """
        for assessor, stage in ASSESSORS:
            with self.subTest(assessor=assessor):
                runner = StubRunner(crashed=[stage])  # verdict defaults to PASS
                with self.assertRaises(AllAttemptsCrashed):
                    self._spec(runner)
                self.assertNotIn("spec approved", self._log)
                last = runner.results[-1]
                self.assertFalse(last.ok)
                self.assertIn("VERDICT: pass", last.output)
                self.assertEqual(self._reasons, [])
                # `_run` exhausted its crash retries before it raised
                self.assertEqual(runner.calls.count(stage.value), 3)

    def test_nonzero_exit_assessor_with_pass_output_parks(self):
        for assessor, stage in ASSESSORS:
            with self.subTest(assessor=assessor):
                runner = StubRunner(bad_rc=[stage])  # rc != 0, not a crash
                self.assertFalse(self._spec(runner))
                self.assertNotIn("spec approved", self._log)
                self.assertIn("VERDICT: pass", runner.results[-1].output)
                self.assertIn("process failure", self._reasons[0])
                # no crash, so no retry: one session, then park
                self.assertEqual(runner.calls.count(stage.value), 1)

    # ------------------------------------------------------------------
    # G3: the kickback loop is untouched — counter, artifacts, maximum
    # ------------------------------------------------------------------
    def test_kickback_returns_to_the_author_then_approves(self):
        runner = StubRunner(sequences={Stage.SPEC_ASSESS_ORNITH: [Verdict.KICKBACK,
                                                                 Verdict.PASS]})
        self.assertTrue(self._spec(runner))
        self.assertEqual(runner.calls, [Stage.SPEC_AUTHOR.value,
                                        Stage.SPEC_ASSESS_ORNITH.value,
                                        Stage.SPEC_AUTHOR.value,
                                        Stage.SPEC_ASSESS_ORNITH.value,
                                        Stage.SPEC_ASSESS_TW.value])
        self.assertIn("kickback to spec author (#1)", self._log)
        self.assertIn("spec approved", self._log)
        self.assertTrue((self.task_dir / "artifacts" / "kickback_ornith_1.md").exists())

    def test_kickback_counter_is_shared_between_the_assessors(self):
        runner = StubRunner(sequences={
            Stage.SPEC_ASSESS_ORNITH: [Verdict.KICKBACK, Verdict.PASS],
            Stage.SPEC_ASSESS_TW: [Verdict.KICKBACK, Verdict.PASS],
        })
        self.assertTrue(self._spec(runner))
        artifacts = sorted(p.name for p in (self.task_dir / "artifacts").glob("kickback_*"))
        self.assertEqual(artifacts, ["kickback_ornith_1.md", "kickback_tw_2.md"])
        self.assertIn("kickback to spec author (#2)", self._log)

    def test_kickback_maximum_parks_and_is_not_exceeded(self):
        runner = StubRunner(sequences={Stage.SPEC_ASSESS_ORNITH: [Verdict.KICKBACK]})
        self.assertFalse(self._spec(runner, max_spec_kickbacks=3))
        # four ornith assessments: three counted kickbacks, the fourth trips the cap
        self.assertEqual(runner.calls.count(Stage.SPEC_ASSESS_ORNITH.value), 4)
        self.assertEqual(runner.calls.count(Stage.SPEC_AUTHOR.value), 4)
        self.assertNotIn(Stage.SPEC_ASSESS_TW.value, runner.calls)
        self.assertEqual(self._reasons, ["spec kickback loop exceeded (3)"])
        self.assertNotIn("spec approved", self._log)
        artifacts = sorted(p.name for p in (self.task_dir / "artifacts").glob("kickback_*"))
        self.assertEqual(artifacts, ["kickback_ornith_1.md", "kickback_ornith_2.md",
                                     "kickback_ornith_3.md"])


class SpecAssessmentProtocolTest(unittest.TestCase):
    """The protocol itself, one session result in, one decision out."""

    def _result(self, verdict: Verdict, ok: bool = True,
                crashed: bool = False) -> SessionResult:
        """`out_file` is never read by `assess_spec`, so it stays a bare path."""
        return SessionResult(ok=ok, verdict=verdict, peak_tokens=0, duration_s=0.0,
                             output=f"VERDICT: {verdict.value}",
                             out_file=Path("unused.out"), crashed=crashed)

    def test_only_a_healthy_pass_approves(self):
        for assessor, _stage in ASSESSORS:
            decision = assess_spec(assessor, self._result(Verdict.PASS))
            self.assertIs(decision.outcome, SpecAssessment.APPROVED)
            self.assertEqual(decision.reason, "")

    def test_kickback_is_the_revision_signal(self):
        decision = assess_spec("ornith", self._result(Verdict.KICKBACK))
        self.assertIs(decision.outcome, SpecAssessment.KICKBACK)
        self.assertEqual(decision.reason, "")

    def test_every_other_verdict_parks_with_assessor_and_verdict(self):
        for verdict in NOT_AN_APPROVAL:
            decision = assess_spec("tw", self._result(verdict))
            self.assertIs(decision.outcome, SpecAssessment.PARKED)
            self.assertIn("tw", decision.reason)
            self.assertIn(verdict.value, decision.reason)

    def test_process_failure_parks_before_the_verdict_is_read(self):
        for crashed in (True, False):
            with self.subTest(crashed=crashed):
                decision = assess_spec("ornith", self._result(Verdict.PASS, ok=False,
                                                              crashed=crashed))
                self.assertIs(decision.outcome, SpecAssessment.PARKED)
                self.assertIn("process failure", decision.reason)
                self.assertIn("ornith", decision.reason)


if __name__ == "__main__":
    unittest.main()
