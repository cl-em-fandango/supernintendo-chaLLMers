"""T57: when every crash retry is exhausted the task is parked.

`Pipeline._run` used to retry a crashed session `maxCrashRetries` times and
then hand the last dead `SessionResult` back to the calling stage, which routed
on a verdict read out of a process that never finished. The exhaustion is now a
signal of its own: `_run` raises `AllAttemptsCrashed` carrying the task id, the
stage and the number of attempts made, and `Pipeline.process` catches it once —
no stage catches it — and parks with that reason.

These tests pin, without a subprocess:
- the exact attempt count: `maxCrashRetries + 1` sessions, never one more;
- the payload: task id, stage and count on the exception;
- the exact park reason, and that it reaches `parked/` exactly once;
- a crash that recovers on a retry still costs no park;
- the catch site is `process` alone.

Out of scope (T74/T75): over-context-budget results, retry-count changes, and
non-crash verdict routing.

Run from the repo root:  python3 -m unittest tests.test_all_attempts_crashed
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
from harness.core.enums import Stage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import AllAttemptsCrashed, Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle

# `maxCrashRetries` is read from the config raw dict; a bare `Config` has an
# empty one, so the default below is the one the shipped `config.json` states.
DEFAULT_RETRIES = 2
DEFAULT_ATTEMPTS = DEFAULT_RETRIES + 1


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _cfg(work_dir: Path, max_crash_retries: int | None = None,
         repo: Path | None = None) -> Config:
    """A config whose crash-retry count is explicit when one is asked for.

    `raw` is what `Config.get("maxCrashRetries", 2)` reads, so passing the
    number here is the only way to move `Pipeline.max_crash_retries`.
    """
    raw = {} if max_crash_retries is None else {"maxCrashRetries": max_crash_retries}
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
        repo_dir=repo,
        raw=raw,
    )


def _make_repo(root: Path) -> Path:
    """A git repo with one commit on `pi/trunk` — all `ensure_branch` needs.

    Deliberately not a copy of the harness tree: every scenario here stops at a
    crash before the holistic merge, so the merge gate is never asked a
    question and no `harness.py` has to exist in the workdir.
    """
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


class ScriptedRunner:
    """Stands in for `SessionRunner`: no subprocess, scripted outcome per stage.

    `crash` names the stages whose session comes back crashed on *every* call;
    any other stage returns its scripted (default: passing) verdict. A crashed
    result is built the way `SessionRunner` builds one — `ok=False` and
    `Verdict.ERROR` — so a stage that tried to route on it would be routing on
    the error verdict T57 removes from its reach.
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

    def __init__(self, crash=(), verdicts=None, recover_after=None):
        self.calls: list[str] = []
        self.results: list[SessionResult] = []
        self.crash = {self._key(s) for s in crash}
        self.verdicts = {self._key(k): v for k, v in (verdicts or {}).items()}
        # `recover_after`: stage -> number of leading calls that crash. Used to
        # show a crash that a retry repairs costs nothing.
        self.recover_after = {self._key(k): v
                              for k, v in (recover_after or {}).items()}

    @staticmethod
    def _key(stage) -> str:
        return stage.value if isinstance(stage, Stage) else str(stage)

    def crashes(self, key: str) -> bool:
        """Whether *this* call crashes, judged on the calls recorded so far."""
        if key in self.crash:
            return True
        limit = self.recover_after.get(key)
        if limit is None:
            return False
        return sum(1 for c in self.calls if c == key) < limit

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        key = self._key(stage)
        crashed = self.crashes(key)
        self.calls.append(key)
        verdict = Verdict.ERROR if crashed else self.verdicts.get(
            key, self.DEFAULTS.get(stage, Verdict.PASS))
        output = f"## Summary\nscripted\n\nVERDICT: {verdict.value}"
        out_file = Path(workdir) / f".pi-session-{key}-{len(self.calls)}.out"
        out_file.write_text(output)
        result = SessionResult(ok=not crashed, verdict=verdict, peak_tokens=0,
                               duration_s=0.0, output=output, out_file=out_file,
                               crashed=crashed)
        self.results.append(result)
        return result


class RunExhaustionTest(unittest.TestCase):
    """`Pipeline._run` itself: attempts, payload, and the healthy short circuit."""

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

    def _exhaust(self, runner: ScriptedRunner, stage: Stage,
                 max_crash_retries: int | None = None) -> AllAttemptsCrashed:
        pipeline = self._pipeline(runner, max_crash_retries)
        with self.assertRaises(AllAttemptsCrashed) as caught:
            pipeline._run("m", self.work_repo, "prompt", task_id="t1", stage=stage)
        return caught.exception

    def test_final_crashed_attempt_raises_instead_of_returning(self):
        runner = ScriptedRunner(crash=[Stage.SPEC_AUTHOR])
        exc = self._exhaust(runner, Stage.SPEC_AUTHOR)
        self.assertEqual(len(runner.results), DEFAULT_ATTEMPTS)
        self.assertTrue(all(r.crashed for r in runner.results))
        # The dead result never reaches the caller: nothing to route on.
        self.assertIs(exc.stage, Stage.SPEC_AUTHOR)

    def test_attempt_count_is_max_crash_retries_plus_one(self):
        for retries in (0, 1, 2, 4):
            with self.subTest(maxCrashRetries=retries):
                runner = ScriptedRunner(crash=[Stage.SLICING])
                exc = self._exhaust(runner, Stage.SLICING,
                                    max_crash_retries=retries)
                self.assertEqual(len(runner.calls), retries + 1,
                                 "the runner was called a number of times that "
                                 "does not match the configured retry count")
                self.assertEqual(exc.attempts, retries + 1)
                self.assertEqual(exc.attempts, len(runner.calls))

    def test_payload_carries_task_stage_and_count(self):
        runner = ScriptedRunner(crash=[Stage.FUNC_REVIEW])
        exc = self._exhaust(runner, Stage.FUNC_REVIEW)
        self.assertEqual(exc.task_id, "t1")
        self.assertIs(exc.stage, Stage.FUNC_REVIEW)
        self.assertEqual(exc.attempts, DEFAULT_ATTEMPTS)

    def test_reason_names_the_count_the_stage_and_the_task(self):
        runner = ScriptedRunner(crash=[Stage.SPEC_AUTHOR])
        exc = self._exhaust(runner, Stage.SPEC_AUTHOR)
        self.assertEqual(
            str(exc),
            f"all {DEFAULT_ATTEMPTS} attempts crashed at stage spec_author "
            f"(task t1)")

    def test_reason_of_a_stray_string_stage_is_still_readable(self):
        """`_run` accepts a `Stage | str`; the reason never shows `Stage.X`."""
        runner = ScriptedRunner(crash=["holistic"])
        exc = self._exhaust(runner, "holistic")  # type: ignore[arg-type]
        self.assertEqual(
            str(exc),
            f"all {DEFAULT_ATTEMPTS} attempts crashed at stage holistic (task t1)")

    def test_a_healthy_attempt_short_circuits_the_raise(self):
        """Two crashes then a healthy session: the result is returned, no raise."""
        runner = ScriptedRunner(recover_after={Stage.SPEC_AUTHOR: 2})
        pipeline = self._pipeline(runner)
        result = pipeline._run("m", self.work_repo, "prompt", task_id="t1",
                               stage=Stage.SPEC_AUTHOR)
        self.assertFalse(result.crashed)
        self.assertEqual(result.verdict, Verdict.DONE)
        self.assertEqual(runner.calls.count(Stage.SPEC_AUTHOR.value),
                         DEFAULT_ATTEMPTS)

    def test_retry_lines_are_logged_for_every_retry_but_the_last_attempt(self):
        runner = ScriptedRunner(crash=[Stage.SLICING])
        self._exhaust(runner, Stage.SLICING)
        logged = "\n".join(self.lines)
        self.assertEqual(logged.count("retrying"), DEFAULT_RETRIES,
                         "the last attempt must not announce a retry that "
                         "will not happen")
        self.assertIn("crashed (rc/timeout)", logged)


class SingleCatchSiteTest(unittest.TestCase):
    """No stage catches it: only `process` turns it into a park."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.task_dir = self.work_dir / "active" / "t1"
        (self.task_dir / "artifacts" / "progress").mkdir(parents=True)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.parked: list[tuple[str, str]] = []

    def _pipeline(self, runner: ScriptedRunner) -> Pipeline:
        pipeline = Pipeline(_cfg(self.work_dir), runner, log=lambda *a: None)
        pipeline.lifecycle.park = lambda task_id, reason: self.parked.append(
            (task_id, reason))
        return pipeline

    def test_stage_functions_let_it_through(self):
        for name, stage in (("stage_spec", Stage.SPEC_AUTHOR),
                            ("stage_feasibility", Stage.FEASIBILITY),
                            ("stage_slicing", Stage.SLICING)):
            with self.subTest(stage=name):
                runner = ScriptedRunner(crash=[stage])
                pipeline = self._pipeline(runner)
                ctx = StageContext("t1", self.task_dir, self.work_repo)
                with self.assertRaises(AllAttemptsCrashed):
                    getattr(pipeline, name)(ctx)
                self.assertEqual(self.parked, [],
                                 f"{name} parked instead of letting T57's "
                                 f"exception reach `process`")


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

        Written straight into `active/t1` because the crash scenarios that need
        it intake the task before the runner is consulted.
        """
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self) -> Task:
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _process(self, runner: ScriptedRunner) -> tuple[str, Pipeline]:
        pipeline = Pipeline(self.cfg, runner, log=self.lines.append)
        pipeline.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        parks: list[tuple[str, str]] = []
        real_park = pipeline.lifecycle.park

        def spy(task_id: str, reason: str) -> None:
            parks.append((task_id, reason))
            real_park(task_id, reason)

        pipeline.lifecycle.park = spy
        self.parks = parks
        return pipeline.process(self._task()), pipeline

    def _parked_state(self) -> dict:
        path = self.lifecycle.task_json_path("t1", where="parked")
        self.assertTrue(path.exists(),
                        f"task did not land in parked/: {path}")
        return json.loads(path.read_text())

    def _summary_text(self) -> str:
        return self.lifecycle.review_summary_path("t1").read_text()

    def test_crash_at_the_first_stage_parks_once_with_the_reason(self):
        runner = ScriptedRunner(crash=[Stage.SPEC_AUTHOR])
        status, _ = self._process(runner)
        reason = (f"all {DEFAULT_ATTEMPTS} attempts crashed at stage "
                  f"spec_author (task t1)")
        self.assertEqual(status, "parked")
        self.assertEqual(self.parks, [("t1", reason)])
        self.assertEqual(self._parked_state()["status"], "parked")
        self.assertIn(reason, self._summary_text())
        # three sessions on the dead stage and nothing after it
        self.assertEqual(runner.calls.count(Stage.SPEC_AUTHOR.value),
                         DEFAULT_ATTEMPTS)
        self.assertEqual(
            [c for c in runner.calls if c != Stage.SPEC_AUTHOR.value], [],
            "a stage after the exhausted one ran")

    def test_crash_at_the_holistic_stage_parks_and_never_merges(self):
        """Every stage passes; the holistic session dies on all attempts."""
        runner = ScriptedRunner(crash=[Stage.HOLISTIC])
        status, _ = self._process(runner)
        reason = (f"all {DEFAULT_ATTEMPTS} attempts crashed at stage "
                  f"holistic (task t1)")
        self.assertEqual(status, "parked")
        self.assertEqual(self.parks, [("t1", reason)])
        self.assertEqual(self._parked_state()["status"], "parked")
        self.assertEqual(runner.calls.count(Stage.HOLISTIC.value),
                         DEFAULT_ATTEMPTS)
        # The work stayed on the feature branch: no merge, no completion.
        self.assertFalse((self.queue_dir / "done" / "t1").exists())
        self.assertTrue((self.queue_dir / "parked" / "t1").exists())

    def test_a_crash_that_recovers_costs_no_park(self):
        """One crash, a healthy retry, and the run proceeds on its verdicts."""
        runner = ScriptedRunner(
            recover_after={Stage.SPEC_AUTHOR: 1},
            verdicts={Stage.SLICING: Verdict.FAIL})
        status, _ = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertEqual(len(self.parks), 1)
        reason = self.parks[0][1]
        self.assertNotIn("attempts crashed", reason,
                         "the run was parked for a crash it had already "
                         "recovered from")
        self.assertIn("slicing", reason, f"unexpected park reason: {reason}")
        self.assertEqual(runner.calls.count(Stage.SPEC_AUTHOR.value), 2)

    def test_exhaustion_is_parked_not_failed_even_at_feasibility(self):
        """Feasibility owns the only `failed` route, and a crash is not it.

        `_stage_failed` sends a feasibility *verdict* failure to failed/; an
        exhausted crash never reaches that helper, so the task parks.
        """
        runner = ScriptedRunner(crash=[Stage.FEASIBILITY])
        status, _ = self._process(runner)
        self.assertEqual(status, "parked")
        self.assertFalse((self.queue_dir / "failed" / "t1").exists())
        self.assertEqual(self._parked_state()["checkpointed_stages"], ["spec"])


if __name__ == "__main__":
    unittest.main()
