"""T75: a parked over-cap task renders a handoff in the review file.

T74 taught `Pipeline.process` to catch `OverContextBudget` and park with the
reason string. That reason tells a human *why* the task stopped but not *where*
it stopped or *what to do next*: the stage, the slice, the iteration, the
measured peak, the cap that tripped, the partial session output and how far the
run had checkpointed all lived only on the caught exception, which is discarded
once the park returns.

This module renders that exception into the review file:

- a `Handoff` dataclass carries the fields (stage, slice id, iteration, peak,
  cap, output path, `checkpointed_stages`, `checkpointed_slices`);
- `TaskLifecycle.park` takes an optional `handoff` and, when present, appends a
  `## Handoff` block (one line per field) and a `## Next agent should` block
  ("re-split the work or reduce its context before resume") after the artifacts
  section;
- `Pipeline.process` builds the handoff from the caught exception plus the
  resume position read from `task.json` and passes it to `park`.

The contract that must not move: **a park without a handoff renders exactly
what it rendered before** — the handoff is purely additive, so every other
terminal summary (plain park, fail, complete) is byte-identical to today.

These tests pin, without a subprocess or a model:
- a plain park has neither new section and is byte-identical to the pre-T75
  summary (the timestamp is frozen so the comparison is exact);
- a handoff park contains both sections, every field line, in the right order,
  with enums rendered as wire values and `None`/empty lists as `none`;
- the real `Pipeline.process` over-cap park renders the handoff for both an
  early trip (no checkpoints yet) and a slice-stage trip (stages checkpointed).

Out of scope (T74): raising `OverContextBudget`, the no-retry rule and the park
decision (`tests/test_over_cap_park.py`); the stream trip (T48), the stats rows
(T49), the cap value, automatic resume, and every other review-file section.

Run from the repo root:  python3 -m unittest tests.test_over_cap_handoff
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import CheckpointStage, Stage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow import task_lifecycle as tl
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import Handoff, TaskLifecycle

# The configured ceiling and a peak just over it, matching T74's numbers so the
# rendered `peak`/`cap` lines are the ones a real trip produces.
CAP = 60_000
OVER_CAP = 60_001

# A frozen clock: the review file stamps `**Date:**` from `_now()`, so freezing
# it makes the byte-identical comparison exact.
FIXED_NOW = "2026-08-29T00:00:00+00:00"

# The next-agent instruction, verbatim as the ticket specifies it.
NEXT = "re-split the work or reduce its context before resume"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _cfg(work_dir: Path, repo: Path | None = None) -> Config:
    return Config(
        work_dir=work_dir,
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
        repo_dir=repo,
        raw={},
    )


def _make_queue(work_dir: Path) -> Path:
    queue = work_dir / "queue"
    for sub in ("pending", "active", "done", "failed", "parked", "review"):
        (queue / sub).mkdir(parents=True)
    return queue


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


def _freeze_clock(test: unittest.TestCase) -> None:
    """Patch `task_lifecycle._now` for the duration of one test."""
    original = tl._now
    tl._now = lambda: FIXED_NOW
    test.addCleanup(setattr, tl, "_now", original)


def _base_summary(task_id: str, td: Path, original: str, reason: str) -> str:
    """The review file a park renders with no handoff — the pre-T75 template,
    reconstructed independently of the implementation so a change to it breaks
    the byte-identical test."""
    return (
        f"# Task: {task_id}\n"
        "\n"
        "**Status:** PARKED\n"
        f"**Date:** {FIXED_NOW}\n"
        "\n"
        "## Original requirement\n"
        "\n"
        f"{original}\n"
        "\n"
        "## Executive summary\n"
        "\n"
        f"{reason}\n"
        "\n"
        "## Artifacts\n"
        "\n"
        f"- spec: `{td}/artifacts/spec.md`\n"
        f"- slices: `{td}/artifacts/slices.md`\n"
        f"- journey: `{td}/artifacts/journey.md`\n"
        f"- session transcripts: `{td}/artifacts/sessions/`\n"
    )


def _handoff_block(stage, slice_id, iteration, peak, cap, out,
                   checkpointed_stages, checkpointed_slices) -> str:
    """The `## Handoff` + `## Next agent should` block, reconstructed
    independently of `_handoff_section` (one line per field, `none` for empty)."""
    return (
        "\n"
        "## Handoff\n"
        "\n"
        f"- stage: {stage}\n"
        f"- slice: {slice_id}\n"
        f"- iteration: {iteration}\n"
        f"- peak: {peak}\n"
        f"- cap: {cap}\n"
        f"- output: {out}\n"
        f"- checkpointed_stages: {checkpointed_stages}\n"
        f"- checkpointed_slices: {checkpointed_slices}\n"
        "\n"
        "## Next agent should\n"
        "\n"
        f"{NEXT}\n"
    )


class PlainParkUnchangedTest(unittest.TestCase):
    """A park with no handoff is byte-identical to the pre-T75 summary."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = _make_queue(self.work_dir)
        self.cfg = _cfg(self.work_dir)
        self.lifecycle = TaskLifecycle(self.cfg, log=lambda *a: None)
        self.body = "# t1\n\nplain park, no repo referenced\n"
        self.lifecycle.intake(Task(id="t1", body=self.body, source="directory:t1.md"))
        _freeze_clock(self)

    def test_plain_park_has_neither_new_section(self):
        self.lifecycle.park("t1", "slice fit check loop exceeded")
        text = self.lifecycle.review_summary_path("t1").read_text()
        self.assertNotIn("## Handoff", text)
        self.assertNotIn("## Next agent should", text)
        self.assertNotIn(NEXT, text)

    def test_plain_park_is_byte_identical_to_today(self):
        reason = "slice fit check loop exceeded"
        self.lifecycle.park("t1", reason)
        td = self.queue / "parked" / "t1"
        expected = _base_summary("t1", td, self.body, reason)
        actual = self.lifecycle.review_summary_path("t1").read_text()
        self.assertEqual(actual, expected)

    def test_plain_park_still_lands_in_parked(self):
        self.lifecycle.park("t1", "reason")
        self.assertTrue((self.queue / "parked" / "t1").exists())
        self.assertFalse((self.queue / "active" / "t1").exists())


class HandoffRenderingTest(unittest.TestCase):
    """A park with a handoff appends both sections, every field, in order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = _make_queue(self.work_dir)
        self.cfg = _cfg(self.work_dir)
        self.lifecycle = TaskLifecycle(self.cfg, log=lambda *a: None)
        self.body = "# t1\n\nover-cap task, no repo referenced\n"
        self.lifecycle.intake(Task(id="t1", body=self.body, source="directory:t1.md"))
        _freeze_clock(self)
        self.out = self.work_dir / "partial-session.out"
        self.out.write_text("partial output")

    def _park_with(self, handoff: Handoff, reason: str) -> str:
        self.lifecycle.park("t1", reason, handoff=handoff)
        return self.lifecycle.review_summary_path("t1").read_text()

    def test_full_handoff_is_base_plus_both_sections(self):
        reason = f"over context budget: peak={OVER_CAP} limit={CAP}"
        handoff = Handoff(
            stage=Stage.SLICE_IMPLEMENT,
            slice_id="2.1",
            iteration=3,
            peak_tokens=OVER_CAP,
            context_limit=CAP,
            output_path=self.out,
            checkpointed_stages=[CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                                 CheckpointStage.SLICING],
            checkpointed_slices=["1", "2"],
        )
        text = self._park_with(handoff, reason)
        td = self.queue / "parked" / "t1"
        expected = _base_summary("t1", td, self.body, reason) + _handoff_block(
            stage="slice_implement", slice_id="2.1", iteration=3,
            peak=OVER_CAP, cap=CAP, out=self.out,
            checkpointed_stages="spec, feasibility, slicing",
            checkpointed_slices="1, 2",
        )
        self.assertEqual(text, expected)

    def test_every_field_line_is_present(self):
        handoff = Handoff(
            stage=Stage.SLICE_IMPLEMENT, slice_id="2.1", iteration=3,
            peak_tokens=OVER_CAP, context_limit=CAP, output_path=self.out,
            checkpointed_stages=[CheckpointStage.SPEC],
            checkpointed_slices=["1"],
        )
        text = self._park_with(handoff, "reason")
        for line in (
            "- stage: slice_implement",
            "- slice: 2.1",
            "- iteration: 3",
            f"- peak: {OVER_CAP}",
            f"- cap: {CAP}",
            f"- output: {self.out}",
            "- checkpointed_stages: spec",
            "- checkpointed_slices: 1",
        ):
            self.assertIn(line, text)

    def test_section_order_is_artifacts_then_handoff_then_next(self):
        handoff = Handoff(stage=Stage.HOLISTIC, peak_tokens=OVER_CAP,
                          context_limit=CAP, output_path=self.out)
        text = self._park_with(handoff, "reason")
        i_art = text.index("## Artifacts")
        i_hand = text.index("## Handoff")
        i_next = text.index("## Next agent should")
        self.assertLess(i_art, i_hand)
        self.assertLess(i_hand, i_next)

    def test_next_agent_line_is_verbatim(self):
        handoff = Handoff(stage=Stage.SLICING, peak_tokens=OVER_CAP,
                          context_limit=CAP, output_path=self.out)
        text = self._park_with(handoff, "reason")
        self.assertIn(f"## Next agent should\n\n{NEXT}\n", text)

    def test_absent_fields_render_as_none_not_the_literal_None(self):
        """A non-slice stage has no slice id, no cap, no output, no checkpoints."""
        handoff = Handoff(stage=Stage.SPEC_AUTHOR, peak_tokens=OVER_CAP)
        text = self._park_with(handoff, "reason")
        self.assertIn("- slice: none", text)
        self.assertIn("- cap: none", text)
        self.assertIn("- output: none", text)
        self.assertIn("- checkpointed_stages: none", text)
        self.assertIn("- checkpointed_slices: none", text)
        self.assertNotIn("None", text)

    def test_a_stray_string_stage_renders_as_is(self):
        """`OverContextBudget.stage` may be a bare string; the line stays clean."""
        handoff = Handoff(stage="holistic", peak_tokens=OVER_CAP,
                          context_limit=CAP, output_path=self.out)
        text = self._park_with(handoff, "reason")
        self.assertIn("- stage: holistic", text)
        self.assertNotIn("Stage.", text)

    def test_handoff_park_still_lands_in_parked(self):
        handoff = Handoff(stage=Stage.SLICING, peak_tokens=OVER_CAP,
                          context_limit=CAP, output_path=self.out)
        self._park_with(handoff, "reason")
        self.assertTrue((self.queue / "parked" / "t1").exists())


class _ScriptedRunner:
    """Stands in for `SessionRunner`: sessions pass by default, the named stage
    trips over the cap on every call (mirrors T74's routing fixture)."""

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

    def __init__(self, trip):
        self.trip = trip.value if isinstance(trip, Stage) else str(trip)
        self.over_out_file: Path | None = None

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        key = stage.value if isinstance(stage, Stage) else str(stage)
        over = key == self.trip
        verdict = self.DEFAULTS.get(stage, Verdict.PASS)
        output = f"## Summary\nscripted\n\nVERDICT: {verdict.value}"
        out_file = Path(workdir) / f".pi-session-{key}.out"
        out_file.write_text(output)
        if over:
            self.over_out_file = out_file
        return SessionResult(
            ok=not over, verdict=verdict,
            peak_tokens=(OVER_CAP if over else 7), duration_s=0.0,
            output=output, out_file=out_file, crashed=False,
            over_context_budget=over, context_limit=(CAP if over else None))


class ProcessHandoffIntegrationTest(unittest.TestCase):
    """`Pipeline.process` builds the handoff from the caught exception + state."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = _make_queue(self.work_dir)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        self.lifecycle = TaskLifecycle(self.cfg, log=lambda *a: None)
        # A `slices.md` so a run that reaches the slice stage has work to trip on.
        td = self.queue / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")
        _freeze_clock(self)

    def _task(self) -> Task:
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _process(self, trip) -> tuple[str, _ScriptedRunner]:
        runner = _ScriptedRunner(trip)
        status = Pipeline(self.cfg, runner, log=lambda *a: None).process(self._task())
        return status, runner

    def _review(self) -> str:
        return self.lifecycle.review_summary_path("t1").read_text()

    def test_early_trip_renders_handoff_with_no_checkpoints(self):
        status, runner = self._process(Stage.SPEC_AUTHOR)
        self.assertEqual(status, "parked")
        text = self._review()
        self.assertIn("## Handoff", text)
        self.assertIn("## Next agent should", text)
        self.assertIn("- stage: spec_author", text)
        self.assertIn("- slice: none", text)
        self.assertIn("- iteration: 1", text)
        self.assertIn(f"- peak: {OVER_CAP}", text)
        self.assertIn(f"- cap: {CAP}", text)
        self.assertIn(f"- output: {runner.over_out_file}", text)
        self.assertIn("- checkpointed_stages: none", text)
        self.assertIn("- checkpointed_slices: none", text)

    def test_slice_trip_renders_handoff_with_checkpointed_stages(self):
        status, runner = self._process(Stage.SLICE_IMPLEMENT)
        self.assertEqual(status, "parked")
        text = self._review()
        self.assertIn("## Handoff", text)
        self.assertIn("- stage: slice_implement", text)
        self.assertIn("- slice: 1", text)
        self.assertIn("- iteration: 1", text)
        self.assertIn(f"- peak: {OVER_CAP}", text)
        self.assertIn(f"- cap: {CAP}", text)
        self.assertIn(f"- output: {runner.over_out_file}", text)
        self.assertIn("- checkpointed_stages: spec, feasibility, slicing", text)
        self.assertIn("- checkpointed_slices: none", text)

    def test_handoff_is_appended_after_an_unchanged_base(self):
        """The over-cap review file is the plain summary plus the handoff block."""
        status, runner = self._process(Stage.SPEC_AUTHOR)
        text = self._review()
        reason = f"over context budget: peak={OVER_CAP} limit={CAP}"
        td = self.queue / "parked" / "t1"
        original = (td / "original.md").read_text()
        base = _base_summary("t1", td, original, reason)
        self.assertTrue(text.startswith(base),
                        "the over-cap review file changed the pre-T75 base")
        self.assertEqual(text[len(base):], _handoff_block(
            stage="spec_author", slice_id="none", iteration=1,
            peak=OVER_CAP, cap=CAP, out=runner.over_out_file,
            checkpointed_stages="none", checkpointed_slices="none"))


if __name__ == "__main__":
    unittest.main()
