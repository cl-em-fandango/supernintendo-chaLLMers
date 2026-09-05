"""T26: per-slice checkpointing inside the `slices` stage (finding F8).

`slices` is one checkpointable stage, so a crash after slice 3 of 5 previously
re-ran slices 1-3 — three implement sessions plus their reviews — for work
already committed on the task branch. `task.json` therefore also carries
`checkpointed_slices`: the ids whose last required review passed, appended as
each slice finishes and consulted before a slice is worked.

The stage-level `CheckpointStage.SLICES` marker is untouched: it answers "is
the whole loop done", the slice list answers "which of these are done", and a
resume uses both.

Sessions are counted by a stub runner and git is never touched — `stage_slices`
is called directly, so this file owns the checkpoint bookkeeping only.

Run from the repo root:  python3 -m unittest tests.test_slice_checkpoint
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import CheckpointStage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle, _parse_completed_slices


class CountingRunner:
    """Stands in for `SessionRunner`: counts sessions per stage and per slice."""

    def __init__(self):
        self.calls: list[str] = []
        self.per_slice: dict[str, int] = {}

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            slice_id=None, **kw) -> SessionResult:
        self.calls.append(stage)
        if slice_id is not None:
            self.per_slice[slice_id] = self.per_slice.get(slice_id, 0) + 1
        out_file = Path(workdir) / f".pi-session-{stage}-{len(self.calls)}.out"
        out_file.write_text("VERDICT: pass")
        # The pipeline compares verdicts with `is` against Verdict members (T29),
        # so the stub must hand back enum members, not plain strings.
        if stage == "slice_implement":
            verdict = Verdict.DONE
        else:
            verdict = Verdict.PASS
        return SessionResult(ok=True, verdict=verdict, peak_tokens=0,
                             duration_s=0.0, output="VERDICT: pass",
                             out_file=out_file, crashed=False)


def _cfg(work_dir: Path) -> Config:
    return Config(
        harness_execution_and_queue_dir=work_dir,
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


class SliceCheckpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = _cfg(Path(self._tmp.name))
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.cfg.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        self.runner = CountingRunner()
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)
        self.pipeline.lifecycle = self.lifecycle
        self.task_dir = self.lifecycle.intake(
            Task(id="t1", body="# t1\n\nrequirement\n", source="directory:t1.md"))
        self.work_repo = Path(self._tmp.name) / "repo"
        self.work_repo.mkdir()
        self._write_slices(["1", "2", "3", "4", "5"])

    def _write_slices(self, ids) -> None:
        body = "# Slices\n" + "".join(f"\n### Slice {s}\n\ndo part {s}\n" for s in ids)
        (self.task_dir / "artifacts" / "slices.md").write_text(body)

    def _ctx(self) -> StageContext:
        return StageContext("t1", self.task_dir, self.work_repo)

    def _raw(self) -> dict:
        # The task may have been moved to parked/ (or another terminal queue
        # subdir) by the pipeline; search where it actually lives.
        for where in ("active", "parked", "failed", "done"):
            path = self.lifecycle.task_json_path("t1", where=where)
            if path.exists():
                return json.loads(path.read_text())
        raise FileNotFoundError("task t1 not found")

    def _log(self) -> str:
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # a full loop records every slice id, in execution order, as strings
    # ------------------------------------------------------------------
    def test_completed_slices_are_recorded_as_they_pass(self):
        self.assertTrue(self.pipeline.stage_slices(self._ctx()))
        self.assertEqual(self._raw()["checkpointed_slices"],
                         ["1", "2", "3", "4", "5"])
        # one implement + two reviews per slice
        self.assertEqual(self.runner.per_slice,
                         {s: 3 for s in ("1", "2", "3", "4", "5")})

    # ------------------------------------------------------------------
    # the card's acceptance case: ["1","2","3"] skips those three, runs 4-5
    # ------------------------------------------------------------------
    def test_resume_skips_checkpointed_slices_and_runs_the_rest(self):
        self.lifecycle.checkpoint_slices("t1", ["1", "2", "3"])
        self.runner = CountingRunner()
        self.pipeline.runner = self.runner

        self.assertTrue(self.pipeline.stage_slices(self._ctx()))

        self.assertEqual(sorted(self.runner.per_slice), ["4", "5"])
        self.assertEqual(self.runner.per_slice["4"], 3)
        self.assertEqual(self.runner.per_slice["5"], 3)
        self.assertIn("⏭ skipping slice 1 (checkpointed)", self._log())
        self.assertIn("⏭ skipping slice 3 (checkpointed)", self._log())
        self.assertNotIn("── slice 2 ──", self._log())
        # the list is append-only: the earlier ids survive, nothing duplicates
        self.assertEqual(self._raw()["checkpointed_slices"],
                         ["1", "2", "3", "4", "5"])

    # ------------------------------------------------------------------
    # a slice checkpoints only after its *last* required review
    # ------------------------------------------------------------------
    def test_slice_not_recorded_when_a_review_fails(self):
        class FailingFuncRunner(CountingRunner):
            def run(self, model, workdir, prompt, *, task_id=None, stage=None,
                    slice_id=None, **kw):
                if stage == "func_review":
                    self.calls.append(stage)
                    out = Path(workdir) / ".pi-session-func.out"
                    out.write_text("VERDICT: fail")
                    return SessionResult(ok=False, verdict="fail", peak_tokens=0,
                                         duration_s=0.0, output="VERDICT: fail",
                                         out_file=out, crashed=False)
                return super().run(model, workdir, prompt, task_id=task_id,
                                   stage=stage, slice_id=slice_id, **kw)

        self.runner = FailingFuncRunner()
        self.pipeline.runner = self.runner
        # the pipeline holds the limit it read at construction; lower it on the
        # pipeline so the func review loop actually terminates
        self.pipeline.cfg.max_slice_func_review = 2

        self.assertFalse(self.pipeline.stage_slices(self._ctx()))
        self.assertEqual(self._raw()["checkpointed_slices"], [])
        self.assertTrue((self.cfg.queue_dir / "parked" / "t1").exists())

    # ------------------------------------------------------------------
    # the stage-level marker is a separate question and stays untouched
    # ------------------------------------------------------------------
    def test_stage_marker_is_independent_of_slice_ids(self):
        self.assertTrue(self.pipeline.stage_slices(self._ctx()))
        state = self.lifecycle.load_state("t1")
        # stage_slices itself records no stage checkpoint...
        self.assertEqual(state.checkpointed_stages, [])
        # ...and the marker stays an enum member, never a slice id
        self.lifecycle.checkpoint("t1", CheckpointStage.SLICES)
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_stages, [CheckpointStage.SLICES])
        self.assertEqual(state.checkpointed_slices, ["1", "2", "3", "4", "5"])

    # ------------------------------------------------------------------
    # old-format task.json (no field, or null) still loads
    # ------------------------------------------------------------------
    def test_old_format_task_json_defaults_to_no_slices(self):
        for payload in (
            {"id": "t1", "status": "active", "source": "s", "created": "now",
             "stage": "spec", "history": []},
            {"id": "t1", "status": "active", "source": "s", "created": "now",
             "stage": "spec", "history": [], "checkpointed_slices": None},
        ):
            (self.task_dir / "task.json").write_text(json.dumps(payload))
            self.assertEqual(self.lifecycle.load_state("t1").checkpointed_slices, [])

    def test_completed_slice_ids_survive_a_round_trip(self):
        self.lifecycle.checkpoint_slices("t1", ["3", "3.10", "3.1"])
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_slices, ["3", "3.10", "3.1"])

    # ------------------------------------------------------------------
    # _parse_completed_slices: order preserved, dedupe keeps first, junk dropped
    # ------------------------------------------------------------------
    def test_parse_completed_slices_rules(self):
        dropped: list[str] = []
        cases = (
            (["3", "3", "1"], ["3", "1"]),
            (["3.10", "3.1"], ["3.10", "3.1"]),
            ([], []),
            (["5", 4, "5"], ["5"]),
        )
        for raw, want in cases:
            self.assertEqual(_parse_completed_slices(raw, dropped.append), want)
        self.assertEqual(len(dropped), 1)

    def test_load_state_normalizes_a_messy_list(self):
        raw = self._raw()
        raw["checkpointed_slices"] = ["3.10", "3.10", 7, "3.1"]
        (self.task_dir / "task.json").write_text(json.dumps(raw))
        state = self.lifecycle.load_state("t1")
        self.assertEqual(state.checkpointed_slices, ["3.10", "3.1"])
        self.assertIn("non-string checkpointed slice", self._log())


if __name__ == "__main__":
    unittest.main()
