"""T76: crossing the context cap hands the work to a clean session, it does not
end the task.

The cap trip (T48), its propagation (T49) and its routing (T74) all treated the
first stop as terminal: `Pipeline._run` raised `OverContextBudget` and
`process` parked the task with a handoff for a *human*. But the stop is a budget
warning — the session's commits, artifacts and partial output are all still
valid work — so parking on the first trip threw away a run that only needed a
fresh context.

The routing now is:

- the first trip writes a handover note
  (`active/<task>/artifacts/progress/handover-<stage>[-slice-<id>]-<n>.md`)
  carrying the stage, slice, iteration, the two numbers, the partial output path
  and the text the stopped session did manage to emit;
- the same stage is then re-run in a **clean session** whose prompt is the
  original stage prompt preceded by a pointer to that note — so the resuming
  session does the same job under the same verdict protocol, and is told to
  check what already landed instead of redoing it;
- a handover that comes back healthy is returned to its stage as if nothing
  happened;
- `OverContextBudget` — and the park — survives only as the exhaustion path,
  after `maxContextContinuations` handovers have all tripped.

These tests pin, without a subprocess or a model:
- one trip + one healthy session = the stage's verdict, no park, no exception;
- the note: its path, its fields, the partial output, and that it is written
  atomically into the task dir;
- the resuming prompt: the note path, the "do not redo" instruction, and the
  original prompt appended verbatim;
- the bound: exactly `maxContextContinuations + 1` sessions before the raise,
  and `maxContextContinuations: 0` restoring the old immediate-raise shape;
- a trip still never costs a crash retry, and a crash still never writes a note;
- `Pipeline.process` end to end: a rescued stage checkpoints, advances and the
  task completes instead of parking.

Out of scope: the stream trip itself (T48,
`tests/test_pi_over_cap_stream.py`), the stats annotation (T49,
`tests/test_over_cap_session.py`), the exhaustion park and the `## Handoff`
review block (T74 `tests/test_over_cap_park.py`, T75
`tests/test_over_cap_handoff.py`), and crash-retry exhaustion (T57).

Run from the repo root:  python3 -m unittest tests.test_over_cap_continuation
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow import continuation as cont
from harness.workflow.continuation import (
    ContinuationNote,
    continuation_prompt,
    handover_dir,
    note_path,
    write_note,
)
from harness.workflow.pipeline import (
    AllAttemptsCrashed,
    OverContextBudget,
    Pipeline,
)
from harness.workflow.task_lifecycle import TaskLifecycle

CAP = 60_000
OVER_CAP = 60_001
CONTINUATIONS = 2


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _cfg(work_dir: Path, continuations: int | None = None,
         max_crash_retries: int | None = None,
         repo: Path | None = None) -> Config:
    raw: dict = {}
    if continuations is not None:
        raw["maxContextContinuations"] = continuations
    if max_crash_retries is not None:
        raw["maxCrashRetries"] = max_crash_retries
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
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


class ScriptedRunner:
    """Stands in for `SessionRunner`, tripping a stage's first `n` sessions.

    `trip` maps a stage to the number of its sessions that come back over the
    cap; after that the stage returns its default (passing) verdict, which is
    how a handover rescue is scripted. A stage absent from `trip` is healthy
    from the first call.
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

    def __init__(self, trip=None, *, crash=(), partial: str = "half a plan"):
        self.trip = {self._key(s): n for s, n in (trip or {}).items()}
        self.crash = {self._key(s) for s in crash}
        self.partial = partial
        self.calls: list[dict] = []

    @staticmethod
    def _key(stage) -> str:
        return stage.value if isinstance(stage, Stage) else str(stage)

    def count(self, stage) -> int:
        key = self._key(stage)
        return sum(1 for c in self.calls if c["stage"] == key)

    def prompts(self, stage) -> list[str]:
        """Every prompt `stage` was sent, in call order."""
        key = self._key(stage)
        return [c["prompt"] for c in self.calls if c["stage"] == key]

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        key = self._key(stage)
        self.calls.append({"stage": key, "prompt": prompt, "kw": dict(kw)})
        seen = self.count(stage)
        over = seen <= self.trip.get(key, 0)
        crashed = key in self.crash
        verdict = (Verdict.ERROR if crashed and not over
                   else self.DEFAULTS.get(stage, Verdict.PASS))
        output = (self.partial + "\n" if over else "") + \
                 f"## Summary\nscripted\n\nVERDICT: {verdict.value}"
        out_file = Path(workdir) / f".pi-session-{key}-{seen}.out"
        out_file.write_text(output)
        return SessionResult(
            ok=not over and not crashed,
            verdict=verdict,
            peak_tokens=(OVER_CAP if over else 7),
            duration_s=0.0,
            output=output,
            out_file=out_file,
            crashed=crashed,
            over_context_budget=over,
            context_limit=(CAP if over else None),
        )


class RescueTest(unittest.TestCase):
    """One trip, one handover, one healthy session: the stage gets its verdict."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue / sub).mkdir(parents=True)
        self.cfg = _cfg(self.work_dir, continuations=CONTINUATIONS)
        self.repo = self.work_dir / "repo"
        self.repo.mkdir()
        self.lines: list[str] = []

    def _pipeline(self, runner: ScriptedRunner,
                  continuations: int | None = None) -> Pipeline:
        cfg = _cfg(self.work_dir, continuations=continuations)
        p = Pipeline(cfg, runner, log=self.lines.append)
        p.lifecycle = TaskLifecycle(cfg, log=self.lines.append)
        return p

    def test_a_rescued_stage_gets_its_verdict_and_nothing_parks(self):
        runner = ScriptedRunner(trip={Stage.SLICING: 1})
        result = self._pipeline(runner)._run("m", self.repo, "SLICE PROMPT",
                                            task_id="t1", stage=Stage.SLICING)
        self.assertEqual(result.verdict, Verdict.DONE)
        self.assertFalse(result.over_context_budget)
        self.assertEqual(runner.count(Stage.SLICING), 2)

    def test_the_resuming_session_is_a_clean_session_on_the_same_stage(self):
        """Same stage, same task, same slice: only the context is new."""
        runner = ScriptedRunner(trip={Stage.SLICE_IMPLEMENT: 1})
        self._pipeline(runner)._run("m", self.repo, "IMPLEMENT",
                                    task_id="t1", stage=Stage.SLICE_IMPLEMENT,
                                    slice_id="2.1", iteration=3)
        self.assertEqual(runner.count(Stage.SLICE_IMPLEMENT), 2)
        first, second = runner.calls[0]["kw"], runner.calls[1]["kw"]
        self.assertEqual(first, second)

    def test_the_resuming_prompt_carries_the_note_and_the_original_prompt(self):
        runner = ScriptedRunner(trip={Stage.SLICING: 1})
        self._pipeline(runner)._run("m", self.repo, "SLICE PROMPT",
                                    task_id="t1", stage=Stage.SLICING)
        first, second = runner.prompts(Stage.SLICING)
        self.assertEqual(first, "SLICE PROMPT")
        # The original prompt survives verbatim: the resuming session is under
        # the same protocol, with the same verdict options.
        self.assertTrue(second.rstrip().endswith("SLICE PROMPT"), second[-80:])
        note = note_path(
            handover_dir(self.queue / "active" / "t1", None),
            Stage.SLICING, None, 1)
        self.assertIn(str(note), second)
        self.assertIn("do not redo completed work", second.lower())
        self.assertIn(str(OVER_CAP), second)

    def test_the_note_lands_in_the_task_dir_and_names_the_stop(self):
        runner = ScriptedRunner(trip={Stage.SLICE_IMPLEMENT: 1},
                                partial="wrote two functions")
        self._pipeline(runner)._run("m", self.repo, "IMPLEMENT",
                                    task_id="t1", stage=Stage.SLICE_IMPLEMENT,
                                    slice_id="2.1", iteration=3)
        note = note_path(
            handover_dir(self.queue / "active" / "t1", None),
            Stage.SLICE_IMPLEMENT, "2.1", 1)
        self.assertTrue(note.exists(), f"no handover note at {note}")
        text = note.read_text()
        self.assertIn("- stage: slice_implement", text)
        self.assertIn("- slice: 2.1", text)
        self.assertIn("- iteration: 3", text)
        self.assertIn("- continuation: 1", text)
        self.assertIn(f"{OVER_CAP} tokens", text)
        self.assertIn(f"cap {CAP}", text)
        self.assertIn("wrote two functions", text)
        self.assertIn("## Next session should", text)

    def test_the_warning_is_logged(self):
        runner = ScriptedRunner(trip={Stage.SLICING: 1})
        self._pipeline(runner)._run("m", self.repo, "p", task_id="t1",
                                    stage=Stage.SLICING)
        log = "\n".join(self.lines)
        self.assertIn("crossed the context cap", log)
        self.assertIn("handing over to a clean session", log)

    def test_two_handovers_then_a_healthy_session(self):
        runner = ScriptedRunner(trip={Stage.SLICING: 2})
        result = self._pipeline(runner)._run("m", self.repo, "p", task_id="t1",
                                            stage=Stage.SLICING)
        self.assertEqual(result.verdict, Verdict.DONE)
        self.assertEqual(runner.count(Stage.SLICING), 3)
        progress = self.queue / "active" / "t1" / "artifacts" / "progress"
        self.assertTrue((progress / "handover-slicing-1.md").exists())
        self.assertTrue((progress / "handover-slicing-2.md").exists())

    def test_a_trip_never_costs_a_crash_retry(self):
        """`maxCrashRetries` does not multiply a tripped session."""
        for retries in (0, 2, 4):
            with self.subTest(maxCrashRetries=retries):
                runner = ScriptedRunner(trip={Stage.SLICING: 1})
                p = self._pipeline(runner)
                p.max_crash_retries = retries
                p._run("m", self.repo, "p", task_id="t1", stage=Stage.SLICING)
                self.assertEqual(runner.count(Stage.SLICING), 2)

    def test_a_crash_writes_no_note(self):
        """The note is for a budget stop; a dead child keeps its own path."""
        runner = ScriptedRunner(crash={Stage.SLICING})
        with self.assertRaises(AllAttemptsCrashed):
            self._pipeline(runner)._run("m", self.repo, "p", task_id="t1",
                                        stage=Stage.SLICING)
        progress = self.queue / "active" / "t1" / "artifacts" / "progress"
        self.assertFalse(progress.exists() and list(progress.glob("handover-*")))

    def test_zero_continuations_raises_on_the_first_trip(self):
        """The bound is honoured down to the old shape: no note, no second run."""
        runner = ScriptedRunner(trip={Stage.SLICING: 1})
        with self.assertRaises(OverContextBudget):
            self._pipeline(runner, continuations=0)._run(
                "m", self.repo, "p", task_id="t1", stage=Stage.SLICING)
        self.assertEqual(runner.count(Stage.SLICING), 1)

    def test_a_bare_session_keeps_its_note_next_to_the_work(self):
        """No task id (as `autonomous.py` runs) still leaves a note on disk."""
        runner = ScriptedRunner(trip={Stage.SLICING: 1})
        self._pipeline(runner)._run("m", self.repo, "p", task_id=None,
                                    stage=Stage.SLICING)
        self.assertTrue(list(self.repo.glob("handover-slicing-1.md")))


class ProcessRescueTest(unittest.TestCase):
    """End to end: a rescued stage advances the waterfall and the task completes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue / sub).mkdir(parents=True)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, continuations=CONTINUATIONS, repo=self.repo)
        self.lines: list[str] = []
        td = self.queue / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self) -> Task:
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _process(self, runner: ScriptedRunner) -> str:
        """`process` with the merge stubbed out.

        The verification gate can only judge the harness repo itself, so the
        real `merge_to_trunk` would refuse this fixture and park for
        `GateNotApplicable` — a reason that has nothing to do with the budget.
        Stubbing the merge lets the completion path be observed.
        """
        pipeline = Pipeline(self.cfg, runner, log=self.lines.append)
        pipeline.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        with patch("harness.core.gitops.merge_to_trunk",
                   lambda *a, **kw: None):
            return pipeline.process(self._task())

    def test_a_trip_rescued_mid_run_completes_instead_of_parking(self):
        runner = ScriptedRunner(trip={Stage.SLICE_IMPLEMENT: 1})
        status = self._process(runner)
        self.assertEqual(status, "done", "\n".join(self.lines))
        self.assertFalse((self.queue / "parked" / "t1").exists())
        self.assertTrue((self.queue / "done" / "t1").exists())
        self.assertEqual(runner.count(Stage.SLICE_IMPLEMENT), 2)
        # The run still advanced: the stages after the rescued one all ran.
        self.assertEqual(runner.count(Stage.TECH_REVIEW), 1)
        self.assertEqual(runner.count(Stage.FUNC_REVIEW), 1)
        self.assertEqual(runner.count(Stage.HOLISTIC), 1)

    def test_the_rescued_run_still_checkpoints_its_slices(self):
        runner = ScriptedRunner(trip={Stage.TECH_REVIEW: 1})
        self._process(runner)
        raw = (self.queue / "done" / "t1" / "task.json").read_text()
        self.assertIn('"slices"', raw)
        self.assertIn('"1"', raw)

    def test_exhaustion_still_parks_with_the_budget_reason(self):
        """The park survives as the last resort, with the T74 reason intact."""
        runner = ScriptedRunner(trip={Stage.SLICING: 99})
        status = self._process(runner)
        self.assertEqual(status, "parked")
        summary = (self.queue / "review" / "t1.md").read_text()
        self.assertIn(f"over context budget: peak={OVER_CAP} limit={CAP}",
                      summary)
        self.assertIn("## Handoff", summary)
        self.assertEqual(runner.count(Stage.SLICING), CONTINUATIONS + 1)


class ContinuationModuleTest(unittest.TestCase):
    """The leaf itself: path, rendering, and the note's own contract."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.task_dir = Path(self._tmp.name) / "active" / "t1"

    def _note(self, **kw) -> ContinuationNote:
        base = dict(stage=Stage.SLICE_IMPLEMENT, attempt=1,
                    peak_tokens=OVER_CAP, context_limit=CAP)
        base.update(kw)
        return ContinuationNote(**base)

    def test_a_task_note_lands_in_the_task_progress_dir(self):
        self.assertEqual(
            handover_dir(self.task_dir, None),
            self.task_dir / "artifacts" / "progress")

    def test_a_note_of_a_session_with_no_task_sits_by_its_output(self):
        """A bare run has no task dir; the note joins the output file."""
        out = Path(self.task_dir).parent / ".pi-session-slicing-1.out"
        self.assertEqual(handover_dir(None, out), out.parent)

    def test_note_path_names_stage_slice_and_attempt(self):
        notes = handover_dir(self.task_dir, None)
        self.assertEqual(
            note_path(notes, Stage.SLICE_IMPLEMENT, "2.1", 2),
            notes / "handover-slice_implement-slice-2.1-2.md")
        self.assertEqual(note_path(notes, Stage.SLICING, None, 1),
                         notes / "handover-slicing-1.md")

    def _write(self, note: ContinuationNote, text: str) -> ContinuationNote:
        return write_note(handover_dir(self.task_dir, None), note, text)

    def test_write_note_returns_the_note_carrying_its_path(self):
        note = self._write(self._note(), "partial text")
        self.assertIsNotNone(note.note_path)
        self.assertTrue(Path(note.note_path).exists())
        self.assertIn("partial text", Path(note.note_path).read_text())

    def test_write_note_leaves_no_temp_file_behind(self):
        self._write(self._note(), "x")
        progress = self.task_dir / "artifacts" / "progress"
        self.assertEqual([p.name for p in progress.iterdir()],
                         ["handover-slice_implement-1.md"])

    def test_a_stray_string_stage_never_renders_as_a_member(self):
        note = self._write(self._note(stage="holistic"), "x")
        text = Path(note.note_path).read_text()
        self.assertIn("- stage: holistic", text)
        self.assertNotIn("Stage.", text)

    def test_an_overlong_transcript_is_tailed_not_dumped(self):
        long_output = "head " + "x" * (cont.PARTIAL_OUTPUT_CHARS * 2)
        note = self._write(self._note(), long_output)
        text = Path(note.note_path).read_text()
        self.assertIn("earlier output truncated", text)
        self.assertNotIn("head xxxx", text)
        self.assertLess(len(text), len(long_output))

    def test_a_silent_session_still_produces_a_readable_note(self):
        note = self._write(self._note(), "   ")
        text = Path(note.note_path).read_text()
        self.assertIn("no text was captured", text)
        self.assertIn("## Next session should", text)

    def test_continuation_prompt_of_a_stage_without_a_slice(self):
        prompt = continuation_prompt("BASE", self._note())
        self.assertIn("BASE", prompt)
        self.assertNotIn("slice ", prompt.split("---")[0])
        self.assertIn("continuation 1", prompt)

    def test_continuation_prompt_of_a_slice_says_which_slice(self):
        prompt = continuation_prompt("BASE", self._note(slice_id="3.1"))
        self.assertIn("(slice 3.1)", prompt)


if __name__ == "__main__":
    unittest.main()
