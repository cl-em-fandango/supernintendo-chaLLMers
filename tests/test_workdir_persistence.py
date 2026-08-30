"""T22 tests: the resolved workdir is recorded at intake, never re-derived (F7).

Run from the repo root:  python3 -m unittest tests.test_workdir_persistence
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
    """A git repo at `root` holding enough of the harness for the merge gate."""
    import shutil as _shutil
    repo_root = Path(__file__).resolve().parent.parent
    root.mkdir(parents=True, exist_ok=True)
    _shutil.copytree(repo_root / "harness", root / "harness", dirs_exist_ok=True,
                     ignore=_shutil.ignore_patterns("__pycache__", "task.json"))
    _shutil.copytree(repo_root / "external", root / "external", dirs_exist_ok=True,
                     ignore=_shutil.ignore_patterns("__pycache__"))
    _shutil.copy(repo_root / "harness.py", root / "harness.py")
    config = {
        "workDir": str(root),
        "logDir": str(root / "logs"),
        "statsDir": str(root / "stats"),
        "tokenBudget": 100000,
        "trunkBranch": "pi/trunk",
    }
    (root / "config.json").write_text(json.dumps(config))
    (root / ".gitignore").write_text(".pi-session-*.out\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root

from harness.core.config import Config
from harness.core.enums import CheckpointStage, Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow import pipeline as pipeline_module
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle


class FakeRunner:
    """Stands in for SessionRunner: records the workdir of every session and
    returns a scripted success verdict."""

    DEFAULTS = {
        "spec_author": Verdict.DONE,
        "spec_assess_ornith": Verdict.PASS,
        "spec_assess_tw": Verdict.PASS,
        "feasibility": Verdict.PASS,
        "slicing": Verdict.DONE,
        "slice_check": Verdict.PASS,
        "slice_implement": Verdict.DONE,
        "tech_review": Verdict.PASS,
        "func_review": Verdict.PASS,
        "holistic": Verdict.PASS,
    }

    def __init__(self, verdicts=None):
        self.calls: list[str] = []
        self.workdirs: list[str] = []
        self.verdicts = {**self.DEFAULTS, **(verdicts or {})}

    def run(self, model, workdir, prompt, *, task_id=None, stage=None,
            slice_id=None, iteration=1, notes=""):
        self.calls.append(stage)
        self.workdirs.append(str(workdir))
        workdir = Path(workdir)
        out_file = workdir / f".pi-session-{stage}-{len(self.calls)}.out"
        verdict = self.verdicts.get(stage, Verdict.PASS)
        out_file.write_text(f"VERDICT: {verdict.value}")
        if stage == "slice_implement" and (workdir / ".git").exists():
            (workdir / "work.md").write_text(f"slice work {len(self.calls)}\n")
            _git(workdir, "add", "-A")
            _git(workdir, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-m", f"slice work {len(self.calls)}")
        return SessionResult(ok=True, verdict=verdict,
                             peak_tokens=0, duration_s=0.0,
                             output=f"VERDICT: {verdict.value}", out_file=out_file,
                             crashed=False)


def _cfg(queue_dir: Path, repo: Path | None = None) -> Config:
    return Config(
        work_dir=queue_dir.parent,
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
    )


class WorkdirPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue_dir = Path(self._tmp.name) / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.lines: list[str] = []
        self.repo = _make_repo(Path(self._tmp.name) / "repo")
        self.cfg = _cfg(self.queue_dir, repo=self.repo)
        self.lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        self.runner = FakeRunner()
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)
        # Count every resolve_workdir call made through the lifecycle.
        self.resolutions: list[Path] = []
        self._real_resolve = self.lifecycle.resolve_workdir

        def spy(task_dir):
            self.resolutions.append(Path(task_dir))
            return self._real_resolve(task_dir)

        self.lifecycle.resolve_workdir = spy
        self.pipeline.lifecycle = self.lifecycle

    def _task(self):
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _seed_slices(self):
        td = self.queue_dir / "active" / "t1"
        (td / "artifacts").mkdir(parents=True, exist_ok=True)
        (td / "artifacts" / "slices.md").write_text(
            "# Slices\n\n### Slice 1\n\ndo the thing\n")

    def _task_json(self, task_id="t1"):
        for where in ("active", "done", "parked", "failed"):
            path = self.lifecycle.task_json_path(task_id, where)
            if path.exists():
                return json.loads(path.read_text())
        raise FileNotFoundError(f"no task.json for {task_id}")

    def _log(self):
        return "\n".join(self.lines)

    # ------------------------------------------------------------------
    # intake records the workdir before any git or session work
    # ------------------------------------------------------------------
    def test_intake_records_workdir_before_ensure_branch(self):
        seen = {}
        real_ensure_branch = pipeline_module.ensure_branch

        def spy(workdir, task_id, trunk):
            seen["workdir"] = str(workdir)
            seen["recorded"] = self._task_json().get("workdir")
            return real_ensure_branch(workdir, task_id, trunk)

        pipeline_module.ensure_branch = spy
        self.addCleanup(setattr, pipeline_module, "ensure_branch", real_ensure_branch)
        self._seed_slices()

        status = self.pipeline.process(self._task())

        self.assertEqual(status, "done")
        # the workdir was resolved exactly once, at intake...
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1"])
        # ...and was already persisted when ensure_branch ran.
        self.assertEqual(seen["recorded"], str(self.repo))
        self.assertEqual(seen["workdir"], str(self.repo))
        self.assertEqual(self._task_json()["workdir"], str(self.repo))

    # ------------------------------------------------------------------
    # a resume reuses the recorded value and never re-derives
    # ------------------------------------------------------------------
    def test_resume_reuses_recorded_workdir(self):
        self.lifecycle.intake(self._task())
        for stage in (CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                      CheckpointStage.SLICING):
            self.lifecycle.checkpoint("t1", stage)
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1"])
        self._seed_slices()
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner

        status = self.pipeline.process(self._task())

        self.assertEqual(status, "done")
        # the resume added no resolution: the saved value was used as-is
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1"])
        self.assertNotIn("resolved from config", self._log())
        self.assertEqual(self.runner.workdirs, [str(self.repo)] * len(self.runner.workdirs))

    # ------------------------------------------------------------------
    # old-format task.json: loads empty, migrates once with a warning
    # ------------------------------------------------------------------
    def test_old_format_state_migrates_once(self):
        td = self.lifecycle.intake(self._task())
        (td / "task.json").write_text(json.dumps({
            "id": "t1", "status": "active", "source": "directory:t1.md",
            "created": "2026-01-01T00:00:00+00:00", "stage": "spec", "history": [],
        }))
        self.assertEqual(self.lifecycle.load_state("t1").workdir, "")
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1"])
        self._seed_slices()

        status = self.pipeline.process(self._task())

        self.assertEqual(status, "done")
        self.assertIn("workdir not recorded for t1, resolved from config",
                      self._log())
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1",
                                            self.queue_dir / "active" / "t1"])
        self.assertEqual(self._task_json()["workdir"], str(self.repo))

    def test_migrated_state_is_deterministic_thereafter(self):
        td = self.lifecycle.intake(self._task())
        (td / "task.json").write_text(json.dumps({
            "id": "t1", "status": "active", "source": "directory:t1.md",
            "created": "2026-01-01T00:00:00+00:00", "stage": "spec", "history": [],
        }))
        # Manually checkpoint past slicing so the process resumes at slices
        # instead of re-running spec/feasibility/slicing.
        for stage in (CheckpointStage.SPEC, CheckpointStage.FEASIBILITY,
                      CheckpointStage.SLICING):
            self.lifecycle.checkpoint("t1", stage)
        self._seed_slices()
        status = self.pipeline.process(self._task())
        # intake resolved once, migration resolved once; no further
        # resolution — the recorded workdir is used thereafter.
        self.assertEqual(status, "done")
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1",
                                            self.queue_dir / "active" / "t1"])
        self.assertIn("workdir not recorded for t1, resolved from config",
                      self._log())
        self.assertEqual(self._task_json()["workdir"], str(self.repo))
        # A subsequent process resumes from the migrated state without
        # re-deriving the workdir.
        self.runner = FakeRunner()
        self.pipeline.runner = self.runner
        # Move the task back to active/ so process() sees the migrated state.
        import shutil as _shutil
        _shutil.move(str(self.queue_dir / "done" / "t1"),
                     str(self.queue_dir / "active" / "t1"))
        pre = len(self.lines)
        status = self.pipeline.process(self._task())
        self.assertEqual(status, "done")
        self.assertEqual(self.resolutions, [self.queue_dir / "active" / "t1",
                                            self.queue_dir / "active" / "t1"])
        self.assertNotIn("resolved from config",
                         "\n".join(self.lines[pre:]))
        self.assertEqual(self.runner.workdirs, [str(self.repo)] * len(self.runner.workdirs))


if __name__ == "__main__":
    unittest.main()
