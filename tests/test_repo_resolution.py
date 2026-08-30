"""Tests for deterministic repo path resolution via config.json and CLI flags.

Verifies:
- repoDir in config.json is loaded and used as the working repository.
- CLI --repo flag overrides config.json.
- Markdown task content is never scraped for repo paths.
- Proper refusal when no repo is configured and task defaults to queue dir.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.cli.parser import parse_args
from harness.composition import build
from harness.core.config import Config, load
from harness.core.enums import Verdict
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("target repo\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


class DummyRunner:
    def __init__(self):
        self.workdirs: list[str] = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, slice_id=None, iteration=1, notes=""):
        self.workdirs.append(str(workdir))
        workdir = Path(workdir)
        out_file = workdir / f".pi-session-{stage}-1.out"
        out_file.write_text(f"VERDICT: {Verdict.PASS.value}")
        return SessionResult(
            ok=True, verdict=Verdict.PASS, peak_tokens=0, duration_s=0.0,
            output=f"VERDICT: {Verdict.PASS.value}", out_file=out_file, crashed=False
        )


class RepoResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.work_dir = self.root / "work"
        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = _make_repo(self.root / "my_project_repo")
        self.other_repo = _make_repo(self.root / "other_repo")

    def test_config_json_repo_dir(self):
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps({
            "workDir": str(self.work_dir),
            "repoDir": str(self.repo),
            "trunkBranch": "pi/trunk",
        }))
        cfg = load(cfg_file)
        self.assertEqual(cfg.repo_dir, self.repo.resolve())

    def test_cli_repo_flag_overrides_config(self):
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps({
            "workDir": str(self.work_dir),
            "repoDir": str(self.repo),
            "trunkBranch": "pi/trunk",
        }))
        args = parse_args(["run-task-loop", "--repo", str(self.other_repo)])
        self.assertEqual(args.repo, str(self.other_repo))

        cfg, _, _, _, _, _ = build(cfg_file, repo=args.repo)
        self.assertEqual(cfg.repo_dir, self.other_repo.resolve())

    def test_markdown_paths_are_ignored(self):
        """Even if markdown contains paths to other repos, the configured repoDir is used."""
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps({
            "workDir": str(self.work_dir),
            "repoDir": str(self.repo),
            "trunkBranch": "pi/trunk",
        }))
        cfg, store, runner, provider, pipeline, log = build(cfg_file)
        
        # Markdown mentions another repo and a path in queue
        task = Task(
            id="task-001",
            body=f"# Task\n\nPlease look at {self.other_repo} and {self.queue_dir}/active/task-001\n",
            source="test"
        )
        task_dir = pipeline.lifecycle.intake(task)
        state = pipeline.lifecycle.load_state("task-001")
        # Workdir is strictly self.repo
        self.assertEqual(Path(state.workdir), self.repo.resolve())

    def test_unconfigured_repo_under_queue_parks_cleanly(self):
        """When no repo is configured, resolving to active/ under queue parks with clear message."""
        cfg_file = self.root / "config.json"
        cfg_file.write_text(json.dumps({
            "workDir": str(self.work_dir),
            "trunkBranch": "pi/trunk",
        }))
        cfg, store, runner, provider, pipeline, log = build(cfg_file)
        pipeline.runner = DummyRunner()

        task = Task(id="task-002", body="# Task without repo\nDo something", source="test")
        outcome = pipeline.process(task)
        self.assertEqual(outcome, "parked")
        
        review_file = self.queue_dir / "review" / "task-002.md"
        self.assertTrue(review_file.exists())
        review_text = review_file.read_text()
        self.assertIn("configure repoDir in config.json or pass --repo on the CLI", review_text)


if __name__ == "__main__":
    unittest.main()
