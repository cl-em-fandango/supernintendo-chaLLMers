"""T74 (revised): an over-cap trip is a warning; only exhaustion parks.

T49 lifted the streamed over-cap stop onto `SessionResult.over_context_budget`,
so the routing layer finally had something to route on. Until now it did not:
`Pipeline._run` looked only at `crashed`, so an over-cap result fell straight
through to the calling stage, which read the partial verdict of a session that
was stopped mid-work and acted on it — and a partial `PASS` at a review stage
would have merged work nobody finished.

The routing is now:

- `_run` checks `over_context_budget` **before** the crash-retry branch, so the
  trip outranks a crash on the same result — a session we stopped on purpose is
  not a child that died — and never enters the crash-retry loop;
- the first trip does **not** end the task. `_run` hands the work to a clean
  session under a handover note (`workflow/continuation.py`), so what this
  module pins is the *terminal* path: after `maxContextContinuations` handovers
  have all tripped on the same stage, `OverContextBudget` is raised carrying the
  task id, the stage, the slice id and iteration the call site asked for, the
  measured peak, the cap that stopped the session and the path of its partial
  output;
- `Pipeline.process` catches it once (no stage catches it) and parks with
  `over context budget: peak=<n> limit=<n>`.

The handover itself — the note, the resuming prompt, a continuation that
finishes the stage — is `tests/test_over_cap_continuation.py`.

These tests pin, without a subprocess or a model:
- one tripped session = exactly one runner call at any `maxCrashRetries` (a trip
  is never crash-retried), and one exhausted stage costs exactly
  `maxContextContinuations + 1` sessions;
- the exception payload, including the fields a handoff needs (`slice_id`,
  `iteration`, `out_file`) that `_run` alone knows;
- the exact park reason, one park, and the task in `parked/`;
- no verdict routing: a stage whose session tripped while carrying a *passing*
  verdict neither advances the pipeline, checkpoints a stage or a slice, nor
  merges;
- the crash path (T57) is untouched when no cap is crossed, and a result
  without the trip is still returned to its stage.

Out of scope (T75): the `## Handoff` / `## Next agent should` sections and any
`TaskLifecycle.park()` signature change — this module asserts the reason string
only. Also out of scope: the stream trip itself (T48,
`tests/test_pi_over_cap_stream.py`), the stats annotation (T49,
`tests/test_over_cap_session.py`), crash-retry exhaustion (T57) and unpark.

Run from the repo root:  python3 -m unittest tests.test_over_cap_park
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import ReviewKind, Stage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import (
    AllAttemptsCrashed,
    OverContextBudget,
    Pipeline,
)
from harness.workflow.task_lifecycle import TaskLifecycle

# The configured ceiling (`maxPromptTokens`) T48 trips on and T49 propagates.
# The park reason shows both numbers, so they are pinned here rather than
# derived from the shipped config file.
CAP = 60_000
OVER_CAP = 60_001

# The park reason, exactly as `TaskLifecycle.park` must receive it.
REASON = f"over context budget: peak={OVER_CAP} limit={CAP}"

# `maxCrashRetries` and `maxContextContinuations` are read from the config raw
# dict; a bare `Config` has an empty one, so the defaults below are the ones the
# shipped `config.json` states.
DEFAULT_RETRIES = 2
DEFAULT_ATTEMPTS = DEFAULT_RETRIES + 1
DEFAULT_CONTINUATIONS = 3
# One stopped session plus every handover to a clean session that follows it.
DEFAULT_TRIPPED_SESSIONS = DEFAULT_CONTINUATIONS + 1


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _cfg(work_dir: Path, max_crash_retries: int | None = None,
         max_context_continuations: int | None = None,
         repo: Path | None = None) -> Config:
    """A config whose crash-retry and handover counts are explicit when asked.

    `raw` is what `Config.get("maxCrashRetries", 2)` and
    `Config.get("maxContextContinuations", 3)` read, so passing the numbers here
    is the only way to move `Pipeline.max_crash_retries` and
    `Pipeline.max_context_continuations` — and the only way to show an over-cap
    trip ignores the crash-retry count entirely.
    """
    raw: dict = {}
    if max_crash_retries is not None:
        raw["maxCrashRetries"] = max_crash_retries
    if max_context_continuations is not None:
        raw["maxContextContinuations"] = max_context_continuations
    return Config(
        work_dir=work_dir,
        repo_dir=repo,
        token_budget=CAP,
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
        raw=raw,
    )


def _make_repo(root: Path) -> Path:
    """A git repo with one commit on `pi/trunk` — all `ensure_branch` needs.

    Deliberately not a copy of the harness tree: a run that reached the holistic
    merge would hit `GateNotApplicable`, and the over-cap scenarios must stop
    before the merge for the reason under test, not for the gate.
    """
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


class ScriptedRunner:
    """Stands in for `SessionRunner`: no subprocess, scripted outcome per stage.

    `over_cap` names the stages whose session comes back over the ceiling on
    *every* call; `crash` names the stages whose session comes back crashed. An
    over-cap result is built the way `SessionRunner` builds one — `ok=False`,
    the measured peak, the cap in force — and deliberately keeps the stage's
    *passing* verdict in `verdict`/`output`: a stopped session's partial text
    can say anything, so the only safe pipeline is one that never reads it.
    """

    DEFAULTS = {
        Stage.SPEC_AUTHOR: Verdict.DONE,
        Stage.SPEC_ASSESS_ORNITH: Verdict.PASS,
        Stage.SPEC_ASSESS_TW: Verdict.PASS,
        Stage.FEASIBILITY: Verdict.PASS,
        Stage.SLICING: Verdict.DONE,
        Stage.SLICE_CHECK: Verdict.PASS,
        Stage.SLICE_IMPLEMENT: Verdict.DONE,
        Stage.TECH_REVIEW: Verdict.PASS,
        Stage.FUNC_REVIEW: Verdict.PASS,
        Stage.HOLISTIC: Verdict.PASS,
    }

    def __init__(self, over_cap=(), crash=(), verdicts=None, *,
                 peak: int | None = None, limit: int | None = CAP):
        self.calls: list[dict] = []
        self.results: list[SessionResult] = []
        self.over_cap = {self._key(s) for s in over_cap}
        self.crash = {self._key(s) for s in crash}
        self.verdicts = {self._key(k): v for k, v in (verdicts or {}).items()}
        self.peak = peak
        self.limit = limit

    @staticmethod
    def _key(stage) -> str:
        return stage.value if isinstance(stage, Stage) else str(stage)

    def count(self, stage) -> int:
        """How many sessions ran for `stage` — the count a retry would change."""
        key = self._key(stage)
        return sum(1 for c in self.calls if c["stage"] == key)

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        key = self._key(stage)
        self.calls.append({"stage": key, "task_id": task_id, "kw": dict(kw)})
        over = key in self.over_cap
        crashed = key in self.crash
        verdict = (Verdict.ERROR if crashed and not over
                   else self.verdicts.get(key, self.DEFAULTS.get(stage, Verdict.PASS)))
        output = f"## Summary\nscripted\n\nVERDICT: {verdict.value}"
        out_file = Path(workdir) / f".pi-session-{key}-{len(self.calls)}.out"
        out_file.write_text(output)
        # `peak` is only ever explicit for the boundary case below; a tripped
        # stage reports a peak over the cap, a healthy one a trivial number.
        peak_tokens = (self.peak if self.peak is not None
                       else (OVER_CAP if over else 7))
        result = SessionResult(
            ok=not over and not crashed,
            verdict=verdict,
            peak_tokens=peak_tokens,
            duration_s=0.0,
            output=output,
            out_file=out_file,
            crashed=crashed,
            over_context_budget=over,
            context_limit=self.limit if over else None,
        )
        self.results.append(result)
        return result


class RunTripTest(unittest.TestCase):
    """`Pipeline._run` itself: no crash retry, the payload, the crash path intact."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.lines: list[str] = []

    def _pipeline(self, runner: ScriptedRunner,
                  max_crash_retries: int | None = None) -> Pipeline:
        return Pipeline(_cfg(self.work_dir, max_crash_retries), runner,
                        log=self.lines.append)

    def _trip(self, runner: ScriptedRunner, stage, *, slice_id=None,
              iteration=None, max_crash_retries: int | None = None,
              task_id: str = "t1") -> OverContextBudget:
        """`_run` against a stage scripted to trip on **every** session.

        The stage therefore exhausts its handovers, so this returns the
        `OverContextBudget` that ends the run. A trip that is rescued by a clean
        session is `tests/test_over_cap_continuation.py`.
        """
        kw: dict = {}
        if slice_id is not None:
            kw["slice_id"] = slice_id
        if iteration is not None:
            kw["iteration"] = iteration
        with self.assertRaises(OverContextBudget) as caught:
            self._pipeline(runner, max_crash_retries)._run(
                "m", self.work_repo, "prompt", task_id=task_id, stage=stage, **kw)
        return caught.exception

    # ------------------------------------------------------------------
    # a. never crash-retried
    # ------------------------------------------------------------------
    def test_over_cap_result_raises_instead_of_being_returned(self):
        runner = ScriptedRunner(over_cap=[Stage.SLICE_IMPLEMENT])
        exc = self._trip(runner, Stage.SLICE_IMPLEMENT, slice_id="1", iteration=1)
        self.assertIs(exc.stage, Stage.SLICE_IMPLEMENT)
        # Every session of the exhausted stage is a trip, and none of them ever
        # reaches the caller: there is nothing safe to route on.
        self.assertEqual(len(runner.results), DEFAULT_TRIPPED_SESSIONS)
        self.assertTrue(all(r.over_context_budget for r in runner.results))

    def test_a_trip_never_costs_a_crash_retry_at_any_retry_count(self):
        """The trip bypasses the crash loop; a handover is not a crash retry.

        The session count is therefore `maxContextContinuations + 1` whatever
        `maxCrashRetries` says — a trip that also fell into the crash branch
        would multiply the two loops.
        """
        for retries in (0, 1, 2, 4):
            with self.subTest(maxCrashRetries=retries):
                runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR])
                self._trip(runner, Stage.SPEC_AUTHOR,
                           max_crash_retries=retries)
                self.assertEqual(len(runner.calls), DEFAULT_TRIPPED_SESSIONS,
                                 "an over-cap session was crash-retried")

    def test_over_cap_outranks_a_crash_on_the_same_result(self):
        """T48 keeps the trip distinct from a death; the routing keeps it that way.

        A stopped child also carries a non-zero rc, so a check placed after the
        crash branch would raise `AllAttemptsCrashed` and announce crash retries
        that must not happen.
        """
        runner = ScriptedRunner(over_cap=[Stage.HOLISTIC], crash=[Stage.HOLISTIC])
        exc = self._trip(runner, Stage.HOLISTIC)
        self.assertIsInstance(exc, OverContextBudget)
        self.assertNotIsInstance(exc, AllAttemptsCrashed)
        self.assertEqual(len(runner.calls), DEFAULT_TRIPPED_SESSIONS)

    # ------------------------------------------------------------------
    # b. the payload a handoff needs
    # ------------------------------------------------------------------
    def test_payload_carries_task_stage_slice_iteration_peak_limit_and_output(self):
        runner = ScriptedRunner(over_cap=[Stage.SLICE_IMPLEMENT])
        exc = self._trip(runner, Stage.SLICE_IMPLEMENT,
                         slice_id="2.1", iteration=3)
        self.assertEqual(exc.task_id, "t1")
        self.assertIs(exc.stage, Stage.SLICE_IMPLEMENT)
        self.assertEqual(exc.slice_id, "2.1")
        self.assertEqual(exc.iteration, 3)
        self.assertEqual(exc.peak_tokens, OVER_CAP)
        self.assertEqual(exc.context_limit, CAP)
        # The partial session output of the *last* stopped session, as the
        # runner reported it: the handoff's "last output path" and the only copy
        # of what the model got done.
        self.assertEqual(exc.out_file, runner.results[-1].out_file)
        self.assertTrue(Path(exc.out_file).exists())

    def test_payload_of_a_stage_without_a_slice_reads_as_no_slice(self):
        """Non-slice call sites pass neither `slice_id` nor `iteration`."""
        runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR])
        exc = self._trip(runner, Stage.SPEC_AUTHOR)
        self.assertIsNone(exc.slice_id)
        # The default `SessionRunner.run` itself uses, so the handoff never
        # shows "iteration=None" for a stage that does not iterate.
        self.assertEqual(exc.iteration, 1)

    def test_reason_is_the_measured_peak_and_the_cap(self):
        runner = ScriptedRunner(over_cap=[Stage.SLICING])
        exc = self._trip(runner, Stage.SLICING)
        self.assertEqual(str(exc), REASON)

    def test_reason_of_a_stray_string_stage_is_still_readable(self):
        """`_run` accepts a `Stage | str`; the reason never shows `Stage.X`."""
        runner = ScriptedRunner(over_cap=["holistic"])
        exc = self._trip(runner, "holistic")  # type: ignore[arg-type]
        self.assertEqual(str(exc), REASON)
        self.assertEqual(exc.stage, "holistic")

    # ------------------------------------------------------------------
    # c. what must keep working untouched
    # ------------------------------------------------------------------
    def test_a_result_without_the_trip_is_still_returned_to_its_stage(self):
        """The new check must not swallow healthy sessions — boundary included.

        A peak sitting exactly on the cap is not a trip (T48's boundary): the
        flag is the only thing `_run` consults, so the routing never invents one.
        """
        runner = ScriptedRunner(peak=CAP)
        result = self._pipeline(runner)._run("m", self.work_repo, "prompt",
                                             task_id="t1", stage=Stage.SLICING)
        self.assertFalse(result.over_context_budget)
        self.assertEqual(result.peak_tokens, CAP)
        self.assertEqual(result.verdict, Verdict.DONE)
        self.assertEqual(len(runner.calls), 1)

    def test_the_crash_path_is_unchanged_when_no_cap_is_crossed(self):
        """T57's exhaustion still costs `maxCrashRetries + 1` attempts."""
        runner = ScriptedRunner(crash=[Stage.SLICING])
        with self.assertRaises(AllAttemptsCrashed) as caught:
            self._pipeline(runner)._run("m", self.work_repo, "prompt",
                                        task_id="t1", stage=Stage.SLICING)
        self.assertNotIsInstance(caught.exception, OverContextBudget)
        self.assertEqual(len(runner.calls), DEFAULT_ATTEMPTS)


class StagesNeverCatchTest(unittest.TestCase):
    """No stage catches it and no stage routes on the partial verdict.

    Every session here carries the verdict that stage would *pass* on, so a
    pipeline that read it would visibly move forward — checkpoint a stage,
    advance to the next session, or merge. None of them do.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        # A real intake, so `stage_slices` (which loads `task.json`) and
        # `stage_holistic` (which asks it about the merge checkpoint) see the
        # state a run would really have at that point.
        self.task_dir = TaskLifecycle(self.cfg, log=lambda *a: None).intake(
            Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                 source="directory:t1.md"))
        (self.task_dir / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")
        self.parked: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.sliced_checkpointed: list[list] = []

    def _pipeline(self, runner: ScriptedRunner) -> Pipeline:
        pipeline = Pipeline(self.cfg, runner, log=lambda *a: None)
        pipeline.lifecycle = TaskLifecycle(self.cfg, log=lambda *a: None)
        pipeline.lifecycle.park = lambda task_id, reason, *a, **kw: (
            self.parked.append((task_id, reason)))
        pipeline.lifecycle.complete = lambda task_id, summary, *a, **kw: (
            self.completed.append((task_id, summary)))
        pipeline.lifecycle.checkpoint_slices = lambda task_id, sids, *a, **kw: (
            self.sliced_checkpointed.append(list(sids)))
        return pipeline

    def _ctx(self) -> StageContext:
        return StageContext("t1", self.task_dir, self.repo)

    def test_stage_functions_let_it_through(self):
        for name, stage in (("stage_spec", Stage.SPEC_AUTHOR),
                            ("stage_feasibility", Stage.FEASIBILITY),
                            ("stage_slicing", Stage.SLICING),
                            ("stage_slices", Stage.SLICE_IMPLEMENT),
                            ("stage_holistic", Stage.HOLISTIC)):
            with self.subTest(stage=name):
                self.parked.clear()
                runner = ScriptedRunner(over_cap=[stage])
                pipeline = self._pipeline(runner)
                with self.assertRaises(OverContextBudget):
                    getattr(pipeline, name)(self._ctx())
                self.assertEqual(self.parked, [],
                                 f"{name} parked instead of letting T74's "
                                 f"exception reach `process`")

    def test_a_tripped_spec_author_does_not_reach_the_assessors(self):
        """The author's partial `done` is not an accepted spec."""
        runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR])
        pipeline = self._pipeline(runner)
        with self.assertRaises(OverContextBudget):
            pipeline.stage_spec(self._ctx())
        # Every session is the author: the handovers stay inside the stage that
        # tripped, and no assessor ever sees a spec nobody finished.
        self.assertEqual(runner.count(Stage.SPEC_AUTHOR), DEFAULT_TRIPPED_SESSIONS)
        self.assertEqual(runner.count(Stage.SPEC_ASSESS_ORNITH), 0)
        self.assertEqual(runner.count(Stage.SPEC_ASSESS_TW), 0)

    def test_a_tripped_review_does_not_fix_or_checkpoint_the_slice(self):
        """A partial review `pass` must not close a slice out."""
        runner = ScriptedRunner(over_cap=[Stage.TECH_REVIEW])
        pipeline = self._pipeline(runner)
        with self.assertRaises(OverContextBudget):
            pipeline._review_loop(self._ctx(), "1", ReviewKind.TECH,
                                  Stage.TECH_REVIEW)
        self.assertEqual(runner.count(Stage.TECH_REVIEW), DEFAULT_TRIPPED_SESSIONS)
        self.assertEqual(runner.count(Stage.SLICE_FIX), 0)
        self.assertEqual(runner.count(Stage.FUNC_REVIEW), 0)
        self.assertEqual(self.sliced_checkpointed, [],
                         "a slice was checkpointed on a stopped review")

    def test_a_tripped_holistic_review_never_merges(self):
        """The partial `pass` of a stopped session cannot land work on trunk."""
        trunk_before = _git(self.repo, "rev-list", "--count", "pi/trunk").strip()
        runner = ScriptedRunner(over_cap=[Stage.HOLISTIC])
        pipeline = self._pipeline(runner)
        with self.assertRaises(OverContextBudget):
            pipeline.stage_holistic(self._ctx())
        self.assertEqual(self.completed, [])
        self.assertEqual(
            _git(self.repo, "rev-list", "--count", "pi/trunk").strip(),
            trunk_before, "trunk advanced on a session that was stopped mid-work")


class ProcessParksTest(unittest.TestCase):
    """`process` catches once: one park, exact reason, `parked/` on disk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        self.lines: list[str] = []
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        self._seed_slices()

    def _seed_slices(self):
        """A `slices.md` in the task dir so a run that reaches `slices` has work.

        Written straight into `active/t1` because the trip scenarios intake the
        task before the runner is consulted.
        """
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self) -> Task:
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _process(self, runner: ScriptedRunner) -> str:
        pipeline = Pipeline(self.cfg, runner, log=self.lines.append)
        pipeline.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        parks: list[tuple[str, str]] = []
        real_park = pipeline.lifecycle.park

        def spy(task_id: str, reason: str, *a, **kw) -> None:
            # `*a/**kw` so a future `park(..., handoff=...)` argument (T75)
            # passes straight through: this module asserts the reason only.
            parks.append((task_id, reason))
            real_park(task_id, reason, *a, **kw)

        pipeline.lifecycle.park = spy
        self.parks = parks
        return pipeline.process(self._task())

    def _parked_state(self) -> dict:
        path = self.lifecycle.task_json_path("t1", where="parked")
        self.assertTrue(path.exists(), f"task did not land in parked/: {path}")
        return json.loads(path.read_text())

    def _summary_text(self) -> str:
        return self.lifecycle.review_summary_path("t1").read_text()

    def test_trip_at_the_first_stage_parks_once_with_the_reason(self):
        runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR])
        status = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertEqual(self.parks, [("t1", REASON)])
        self.assertEqual(self._parked_state()["status"], "parked")
        self.assertIn(REASON, self._summary_text())
        # The stopped session plus its handovers, all of them the tripped stage:
        # the run never left that stage, so it never reached a later one.
        self.assertEqual(len(runner.calls), DEFAULT_TRIPPED_SESSIONS,
                         f"the run made {len(runner.calls)} runner calls")

    def test_no_stage_runs_after_the_trip(self):
        runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR])
        self._process(runner)
        self.assertEqual([c["stage"] for c in runner.calls],
                         [Stage.SPEC_AUTHOR.value] * DEFAULT_TRIPPED_SESSIONS,
                         "a stage after the tripped one ran")

    def test_partial_verdict_is_never_routed_on(self):
        """The author's partial `done` earns no checkpoint and no next stage."""
        runner = ScriptedRunner(over_cap=[Stage.SPEC_AUTHOR],
                                verdicts={Stage.SPEC_AUTHOR: Verdict.DONE})
        status = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertEqual(self._parked_state()["checkpointed_stages"], [],
                         "a stage was checkpointed on a session that was "
                         "stopped mid-work")
        self.assertFalse((self.queue_dir / "done" / "t1").exists())

    def test_trip_at_a_slice_stage_parks_without_a_slice_checkpoint(self):
        runner = ScriptedRunner(over_cap=[Stage.SLICE_IMPLEMENT])
        status = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertEqual(self.parks, [("t1", REASON)])
        state = self._parked_state()
        self.assertEqual(state["checkpointed_stages"],
                         ["spec", "feasibility", "slicing"])
        self.assertEqual(state["checkpointed_slices"], [])
        self.assertEqual(runner.count(Stage.SLICE_IMPLEMENT),
                         DEFAULT_TRIPPED_SESSIONS)
        self.assertEqual(runner.count(Stage.TECH_REVIEW), 0)

    def test_trip_at_the_holistic_stage_parks_and_never_merges(self):
        """Every stage passes; the holistic session crosses the cap."""
        trunk_before = _git(self.repo, "rev-list", "--count", "pi/trunk").strip()
        runner = ScriptedRunner(over_cap=[Stage.HOLISTIC])
        status = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertEqual(self.parks, [("t1", REASON)])
        self.assertEqual(self._parked_state()["status"], "parked")
        self.assertEqual(runner.count(Stage.HOLISTIC), DEFAULT_TRIPPED_SESSIONS)
        self.assertFalse((self.queue_dir / "done" / "t1").exists())
        self.assertTrue((self.queue_dir / "parked" / "t1").exists())
        self.assertEqual(
            _git(self.repo, "rev-list", "--count", "pi/trunk").strip(),
            trunk_before, "trunk advanced on a session that was stopped mid-work")

    def test_trip_is_parked_not_failed_even_at_feasibility(self):
        """Feasibility owns the only `failed` route, and an over-cap stop is not it."""
        runner = ScriptedRunner(over_cap=[Stage.FEASIBILITY])
        status = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertFalse((self.queue_dir / "failed" / "t1").exists())
        self.assertEqual(self._parked_state()["checkpointed_stages"], ["spec"])

    def test_the_reason_is_not_the_crash_reason(self):
        """The two park reasons stay tellable apart for whoever reads parked/."""
        runner = ScriptedRunner(over_cap=[Stage.SLICING])
        self._process(runner)
        self.assertEqual(len(self.parks), 1)
        reason = self.parks[0][1]
        self.assertNotIn("attempts crashed", reason, reason)
        self.assertEqual(reason, REASON)


if __name__ == "__main__":
    unittest.main()
