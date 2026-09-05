"""Slice 1 tests: resume-aware Pipeline.process() (spec FR2, acceptance #1/#2/#7).

Run from the repo root:  python3 -m unittest tests.test_pipeline_resume
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_repo(root: Path) -> Path:
    """Create a git repo at `root` with one commit on pi/trunk.

    Copies the harness package + entry point in so the merge verification
    gate (`harness.py status` inside the workdir) passes.
    """
    import shutil as _shutil
    repo_root = Path(__file__).resolve().parent.parent
    root.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(repo_root / "harness", root / "harness", dirs_exist_ok=True,
                     ignore=_shutil.ignore_patterns("__pycache__", "task.json"))
    _shutil.copytree(repo_root / "external", root / "external", dirs_exist_ok=True,
                     ignore=_shutil.ignore_patterns("__pycache__"))
    _shutil.copy(repo_root / "harness.py", root / "harness.py")
    config = {
        "harnessExecutionAndQueueDir": str(root),
        "logDir": str(root / "logs"),
        "statsDir": str(root / "stats"),
        "tokenBudget": 100000,
        "trunkBranch": "pi/trunk"
    }
    (root / "config.json").write_text(json.dumps(config))
    (root / ".gitignore").write_text(".pi-session-*.out\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root

from harness.core.config import Config
from harness.core.enums import CheckpointStage
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle


class FakeRunner:
    """Stands in for SessionRunner: records every stage it is asked to run
    and returns a scripted verdict per stage (default: success)."""

    # Default verdicts per stage, matching the real pipeline contract.
    DEFAULTS = {
        "spec_author": "done",
        "spec_assess_ornith": "pass",
        "spec_assess_tw": "pass",
        "feasibility": "pass",
        "slicing": "done",
        "slice_check": "pass",
        "slice_implement": "done",
        "tech_review": "pass",
        "func_review": "pass",
        "holistic": "pass",
    }

    def __init__(self, verdicts=None, sequences=None):
        self.calls: list[str] = []
        self.verdicts = {**self.DEFAULTS, **(verdicts or {})}
        # sequences: stage -> list of verdicts, consumed in order (one per call)
        self.sequences = dict(sequences or {})
        self._seq_index: dict[str, int] = {}

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            slice_id=None, iteration=1, notes=""):
        self.calls.append(stage)
        if stage in self.sequences:
            i = self._seq_index.get(stage, 0)
            seq = self.sequences[stage]
            verdict = seq[i] if i < len(seq) else seq[-1]
            self._seq_index[stage] = i + 1
        else:
            verdict = self.verdicts.get(stage, "pass")
        workdir = Path(workdir)
        out_file = workdir / f".pi-session-{stage}-{len(self.calls)}.out"
        out_file.write_text("VERDICT: " + verdict)
        # Simulate the model doing real work: commit a change so the
        # holistic squash-merge has something to merge.
        if stage == "slice_implement" and (workdir / ".git").exists():
            (workdir / "work.md").write_text(f"slice work {len(self.calls)}\n")
            _git(workdir, "add", "-A")
            _git(workdir, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-m", f"slice work {len(self.calls)}")
        from harness.core.enums import Verdict
        return SessionResult(ok=True, verdict=Verdict.parse(verdict) or Verdict.UNKNOWN,
                             peak_tokens=0, duration_s=0.0,
                             output="VERDICT: " + verdict, out_file=out_file,
                             crashed=False)


def _cfg(queue_dir: Path, repo: Path | None = None) -> Config:
    return Config(
        harness_execution_and_queue_dir=queue_dir.parent,
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
        target_codebase_dir=repo,
    )


class PipelineResumeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.repo = _make_repo(Path(self._tmp.name) / "repo")
        self.cfg = _cfg(self.queue_dir, repo=self.repo)
        self.runner = FakeRunner()
        self.pipeline = Pipeline(self.cfg, self.runner,
                                 log=self.lines.append)
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        self._seed_slices()

    def _seed_slices(self):
        """Write a minimal slices.md so stage_slices has work to do."""
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task(self, workdir=None):
        body = f"# t1\n\nwork in {workdir or self.repo}\n"
        return Task(id="t1", body=body, source="directory:t1.md")

    def _task_json(self, task_id="t1"):
        for where in ("active", "done", "parked", "failed"):
            path = self.lifecycle.task_json_path(task_id, where)
            if path.exists():
                return json.loads(path.read_text())
        raise FileNotFoundError(f"no task.json for {task_id} in any queue dir")

    # ------------------------------------------------------------------
    # acceptance #1: fresh run checkpoints all four stages
    # ------------------------------------------------------------------
    def test_fresh_run_checkpoints_all_stages(self):
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        raw = self._task_json()
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices", "merge"])
        self.assertGreaterEqual(raw["last_updated"], raw["created"])
        # every stage ran exactly once
        for stage in ("spec_author", "feasibility", "slicing", "slice_implement"):
            self.assertIn(stage, self.runner.calls)
        # task moved to done/
        self.assertTrue((self.queue_dir / "done" / "t1" / "task.json").exists())

    # ------------------------------------------------------------------
    # acceptance #2: re-entry skips checkpointed stages
    # ------------------------------------------------------------------
    def test_resume_skips_checkpointed_stages(self):
        # Simulate a crash mid-stage_slices: spec/feasibility/slicing completed
        # (checkpointed), slices did not. The task dir sits in active/.
        self.lifecycle.intake(self._task())
        for stage in (CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                      CheckpointStage.SLICING):
            self.lifecycle.checkpoint("t1", stage)
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        # no upstream stage re-ran
        self.assertNotIn("spec_author", self.runner.calls)
        self.assertNotIn("feasibility", self.runner.calls)
        self.assertNotIn("slicing", self.runner.calls)
        # only slices + holistic ran
        self.assertIn("slice_implement", self.runner.calls)
        self.assertIn("holistic", self.runner.calls)
        # skip lines were logged
        joined = "\n".join(self.lines)
        self.assertIn("resuming from checkpoint — skipping: spec, feasibility, slicing", joined)
        self.assertIn("⏭ skipping spec (checkpointed)", joined)
        self.assertIn("⏭ skipping feasibility (checkpointed)", joined)
        self.assertIn("⏭ skipping slicing (checkpointed)", joined)
        self.assertIn("▶ slices", joined)
        # task reached done/ with all four stages checkpointed
        raw = json.loads((self.queue_dir / "done" / "t1" / "task.json").read_text())
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices", "merge"])

    # ------------------------------------------------------------------
    # acceptance #7: old-format task.json resumes from spec
    # ------------------------------------------------------------------
    def test_old_format_task_json_resumes_from_spec(self):
        td = self.lifecycle.intake(self._task())
        (td / "task.json").write_text(json.dumps({
            "id": "t1", "status": "active", "source": "directory:t1.md",
            "created": "2026-01-01T00:00:00+00:00", "stage": "spec", "history": [],
        }))
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertIn("spec_author", self.runner.calls)
        self.assertIn("feasibility", self.runner.calls)

    # ------------------------------------------------------------------
    # EC2: corrupt task.json resumes from spec without crashing
    # ------------------------------------------------------------------
    def test_corrupt_task_json_resumes_from_spec(self):
        td = self.lifecycle.intake(self._task())
        (td / "task.json").write_text("{corrupt")
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertIn("spec_author", self.runner.calls)
        # the corrupt file was replaced with a valid one (F1.4.1)
        raw = self._task_json()
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices", "merge"])

    # ------------------------------------------------------------------
    # EC10: feasibility kickback re-completing spec keeps the list a prefix
    # ------------------------------------------------------------------
    def test_feasibility_kickback_keeps_checkpoint_prefix(self):
        # spec passes, feasibility kicks back, spec re-runs, feasibility passes
        self.runner = FakeRunner(sequences={"feasibility": ["kickback", "pass"]})
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        raw = self._task_json()
        self.assertEqual(raw["checkpointed_stages"],
                         ["spec", "feasibility", "slicing", "slices", "merge"])
        # spec ran twice (initial + kickback), feasibility twice (kick + recheck)
        self.assertEqual(self.runner.calls.count("spec_author"), 2)
        self.assertEqual(self.runner.calls.count("feasibility"), 2)

    # ------------------------------------------------------------------
    # terminal contract: feasibility kickout -> failed, others -> parked
    # ------------------------------------------------------------------
    def test_feasibility_kickout_returns_failed(self):
        self.runner = FakeRunner(verdicts={"feasibility": "kickout"})
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "failed")
        self.assertTrue((self.queue_dir / "failed" / "t1").exists())
        raw = json.loads(self.lifecycle.task_json_path("t1", where="failed").read_text())
        self.assertEqual(raw["checkpointed_stages"], ["spec"])

    def test_slicing_failure_parks(self):
        self.runner = FakeRunner(verdicts={"slicing": "fail"})
        self.pipeline.runner = self.runner
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "parked")
        self.assertTrue((self.queue_dir / "parked" / "t1").exists())
        raw = json.loads(self.lifecycle.task_json_path("t1", where="parked").read_text())
        self.assertEqual(raw["checkpointed_stages"], ["spec", "feasibility"])

    # ------------------------------------------------------------------
    # EC13: missing original.md -> resolve_workdir falls back, no crash
    # ------------------------------------------------------------------
    def test_missing_original_md_does_not_crash(self):
        # EC13: original.md missing -> process() completes without raising.
        td = self.lifecycle.intake(self._task())
        (td / "original.md").unlink()
        status = self.pipeline.process(self._task())
        self.assertIn(status, ("done", "parked"))


if __name__ == "__main__":
    unittest.main()
