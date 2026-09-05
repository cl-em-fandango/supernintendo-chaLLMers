"""Slice 5 — placeholder app + pre-spec hook (FR-2, FR-6.2, FR-8.2; AC 2, 7, 8).

Three layers, all in-process:

  * the pipeline hook site: a demo task fires `placeholder_hook` before
    `stage_spec` (order asserted on one recorded call sequence); a non-demo
    task, a resume past the spec checkpoint, and an unwired hook fire
    nothing; a raising hook never blocks spec (FR-2.3);
  * the hook itself with a fake deployer + fake API: the placeholder page
    carries the issue title and the "is in flight" sentence, the deployer
    receives the app directory, success/failure comments land on the fake
    issue, collisions append `-<issue-number>`, and title text is escaped;
  * the hook with the real Slice 4 deployer against a `git init --bare`
    fake origin: `pi/app-demo`'s `docs/` holds only the placeholder
    artifacts.

Composition gating: the hook factory returns None when `demo.enabled` is
false or GitHub is unconfigured. No real `pi`, no network, no npm.

Run from the repo root:  python3 -m unittest tests.test_demo_placeholder_hook
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.demo_deploy import DemoDeployError, DeployStep
from harness.composition import build_placeholder_hook
from harness.core.config import Config
from harness.core.enums import CheckpointStage
from harness.core.providers import Task
from tests.legacy_sidecars import (
    SyncLinkage,
    task_dir_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.demo_placeholder import (
    DemoPlaceholderHook,
    PlaceholderDeployParams,
    placeholder_issue_of,
    render_placeholder_page,
    write_placeholder_app,
)
from harness.workflow.pipeline import Pipeline

REPO = "acme/widgets"
TITLE = "Pizza Fan Site"


def _cfg(work_dir: Path, repo: Path | None = None) -> Config:
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
        target_codebase_dir=repo,
    )


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com",
         "-c", "user.name=Test", *args],
        cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def _make_repo(root: Path, with_origin: Path | None = None) -> Path:
    """A git repo with one commit on `pi/trunk` (and an optional origin)."""
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    if with_origin is not None:
        _git(root, "remote", "add", "origin", str(with_origin))
    return root


# ---------------------------------------------------------------------------
# layer 1: the pipeline hook site
# ---------------------------------------------------------------------------

class RecordingPipeline(Pipeline):
    """Every stage records its name instead of running sessions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    def stage_spec(self, ctx):
        self.calls.append("spec")
        return True

    def stage_feasibility(self, ctx):
        self.calls.append("feasibility")
        return True

    def stage_slicing(self, ctx):
        self.calls.append("slicing")
        return True

    def stage_slices(self, ctx):
        self.calls.append("slices")
        return True

    def stage_holistic(self, ctx):
        self.calls.append("holistic")
        return "done"


class PipelineHookSiteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        self.messages: list[str] = []

    def pipeline(self, hook):
        return RecordingPipeline(self.cfg, runner=object(),
                                 log=self.messages.append,
                                 placeholder_hook=hook)

    def test_demo_task_fires_placeholder_before_spec(self):
        calls: list[str] = []
        seen: list[tuple] = []

        def hook(task, workdir):
            calls.append("placeholder")
            seen.append((task.id, Path(workdir)))

        pipeline = self.pipeline(hook)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(calls, ["placeholder"])
        self.assertEqual(pipeline.calls[0], "spec")
        # one recorded sequence: the placeholder precedes the spec stage
        self.assertEqual(
            ["placeholder", *pipeline.calls],
            ["placeholder", "spec", "feasibility", "slicing", "slices",
             "holistic"])
        self.assertEqual(seen, [("demo_app", self.repo)])
        self.assertEqual(outcome, "done")

    def test_non_demo_task_fires_nothing(self):
        calls: list[str] = []
        pipeline = self.pipeline(lambda task, workdir: calls.append("x"))
        pipeline.process(Task(id="plain_task", body="b"))
        self.assertEqual(calls, [])

    def test_unwired_hook_is_a_no_op(self):
        pipeline = self.pipeline(None)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "done")

    def test_resume_past_the_spec_checkpoint_does_not_fire(self):
        """FR-1.4 documented limitation: a task that has passed the pre-spec
        hook never retroactively deploys a placeholder."""
        calls: list[str] = []
        pipeline = self.pipeline(lambda task, workdir: calls.append("x"))
        pipeline.lifecycle.intake(Task(id="demo_app", body="b",
                                       meta={"demo": True}))
        pipeline.lifecycle.checkpoint("demo_app", CheckpointStage.SPEC)
        pipeline.process(Task(id="demo_app", body="b", meta={"demo": True}))
        self.assertEqual(calls, [])
        self.assertNotIn("spec", pipeline.calls)  # stage was checkpointed

    def test_raising_hook_does_not_block_spec(self):
        """FR-2.3: even a hook that ignores its contract cannot cost the
        task its spec work."""
        def broken_hook(task, workdir):
            raise RuntimeError("hook exploded")

        pipeline = self.pipeline(broken_hook)
        pipeline.process(Task(id="demo_app", body="b", meta={"demo": True}))
        self.assertIn("spec", pipeline.calls)
        self.assertTrue(any("placeholder hook failed" in m
                            and "hook exploded" in m for m in self.messages))


# ---------------------------------------------------------------------------
# layer 2: the hook with a fake deployer and fake API
# ---------------------------------------------------------------------------

class FakeApi:
    def __init__(self, title: str = TITLE):
        self.title = title
        self.reads: list = []
        self.comments: list[tuple[int, str]] = []

    def get_issue(self, number):
        self.reads.append(("get_issue", number))

        class _Issue:
            pass

        issue = _Issue()
        issue.title = self.title
        return issue

    def create_comment(self, number, body):
        self.comments.append((number, body))


class HookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.queue_dir = self.root / "queue"
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()
        self.api = FakeApi()
        self.deployed: list = []
        self.messages: list = []

    def params(self) -> PlaceholderDeployParams:
        return PlaceholderDeployParams(
            queue_dir=self.queue_dir,
            apps_dir="demo-apps",
            harness_repo=self.root / "harness",
            deploy_dir=self.root / "deploy",
            deploy_branch="pi/app-demo",
            trunk_branch="pi/trunk",
            docs_dir="docs")

    def linked_task(self, task_id: str = "pizza_fan_site", issue: int = 7,
                    repo: str = REPO) -> Task:
        task_dir = self.queue_dir / "active" / task_id
        task_dir.mkdir(parents=True)
        write_legacy_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=issue, repo=repo, demo=True))
        return Task(id=task_id, body="b", meta={"demo": True})

    def hook(self, deployer=None) -> DemoPlaceholderHook:
        return DemoPlaceholderHook(
            self.params(), self.api,
            deployer=deployer or self.deployed.append,
            origin_resolver=lambda repo: "https://origin.example/repo.git",
            log=self.messages.append)

    def test_success_writes_page_deploys_and_comments(self):
        task = self.linked_task()
        hook = self.hook()

        hook(task, self.workdir)

        # the title is read from the issue, not guessed from the task
        self.assertIn(("get_issue", 7), self.api.reads)
        app_dir = self.workdir / "demo-apps" / "pizza-fan-site"
        page = (app_dir / "index.html").read_text()
        self.assertIn(TITLE, page)
        self.assertIn("is in flight", page)
        # the deployer received exactly the placeholder app directory
        self.assertEqual(len(self.deployed), 1)
        request = self.deployed[0]
        self.assertEqual(request.artifacts_dir, app_dir)
        self.assertEqual(request.deploy_branch, "pi/app-demo")
        self.assertEqual(request.docs_dir, "docs")
        self.assertEqual(request.trunk_branch, "pi/trunk")
        self.assertEqual(request.origin_url, "https://origin.example/repo.git")
        # FR-8.2 success comment with the derived Pages URL
        self.assertEqual(
            self.api.comments,
            [(7, "Placeholder deployed — https://acme.github.io/widgets/")])

    def test_failure_comments_and_never_raises(self):
        """FR-2.3: a deploy failure comments and the hook returns quietly."""
        def failing(request):
            raise DemoDeployError(DeployStep.PUSH, "push rejected")

        task = self.linked_task()
        self.hook(deployer=failing)(task, self.workdir)

        self.assertEqual(len(self.api.comments), 1)
        number, body = self.api.comments[0]
        self.assertEqual(number, 7)
        self.assertTrue(body.startswith("placeholder deployment failed:"),
                        body)
        self.assertIn("push rejected", body)

    def test_unlinked_task_deploys_and_comments_nothing(self):
        task = Task(id="orphan_task", body="b", meta={"demo": True})
        self.hook()(task, self.workdir)
        self.assertEqual(self.deployed, [])
        self.assertEqual(self.api.comments, [])

    def test_name_collision_appends_issue_number(self):
        """Edge case 8: the taken name keeps the old app, ours gets -<issue>."""
        (self.workdir / "demo-apps" / "pizza-fan-site").mkdir(parents=True)
        task = self.linked_task(issue=42)

        self.hook()(task, self.workdir)

        app_dir = self.workdir / "demo-apps" / "pizza-fan-site-42"
        self.assertTrue((app_dir / "index.html").is_file())
        self.assertEqual(self.deployed[0].artifacts_dir, app_dir)

    def test_title_is_escaped_into_the_page(self):
        """Edge case 5: issue text is data, never markup."""
        self.api.title = "Pizza <script> Fan"
        task = self.linked_task()

        self.hook()(task, self.workdir)

        page = (self.workdir / "demo-apps" / "pizza-script-fan"
                / "index.html").read_text()
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_placeholder_page_stamps_the_owning_issue(self):
        """FR-2.4 support: the final generation must be able to tell
        "this bare-named directory is our placeholder" from "another
        issue's app" (edge case 8)."""
        self.hook()(self.linked_task(issue=7), self.workdir)
        app_dir = self.workdir / "demo-apps" / "pizza-fan-site"
        self.assertEqual(placeholder_issue_of(app_dir), 7)

    def test_rendered_page_shape(self):
        """The FR-2.2 acknowledgement: title plus the in-flight sentence
        (quotes around the title are HTML-escaped, the text is intact)."""
        page = render_placeholder_page("Pizza Fan Site")
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn("Pizza Fan Site", page)
        self.assertIn("is in flight", page)
        self.assertIn("The app is being built", page)


class OwnershipMarkerTest(unittest.TestCase):
    def test_marker_round_trips_through_the_written_page(self):
        with tempfile.TemporaryDirectory() as td:
            apps = Path(td) / "demo-apps"
            app_dir = write_placeholder_app(apps, "pizza", TITLE, 42)
            self.assertEqual(placeholder_issue_of(app_dir), 42)

    def test_unstamped_and_missing_directories_report_none(self):
        with tempfile.TemporaryDirectory() as td:
            apps = Path(td) / "demo-apps"
            other = write_placeholder_app(apps, "pizza", TITLE)
            self.assertIsNone(placeholder_issue_of(other))
            self.assertIsNone(placeholder_issue_of(apps / "absent"))


# ---------------------------------------------------------------------------
# layer 3: the real deployer against a fake origin
# ---------------------------------------------------------------------------

class RealDeployerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        _git(self.root, "init", "--bare", "-b", "pi/trunk", str(self.origin))
        self.repo = _make_repo(self.root / "harness", with_origin=self.origin)
        self.queue_dir = self.root / "queue"
        task_dir = self.queue_dir / "active" / "pizza_fan_site"
        task_dir.mkdir(parents=True)
        write_legacy_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        self.api = FakeApi()

    def origin_tree(self, branch: str) -> list[str]:
        return sorted(_git(self.origin, "ls-tree", "-r", "--name-only",
                           branch).split())

    def test_placeholder_lands_in_docs_on_the_deploy_branch(self):
        hook = DemoPlaceholderHook(
            PlaceholderDeployParams(
                queue_dir=self.queue_dir,
                apps_dir="demo-apps",
                harness_repo=self.repo,
                deploy_dir=self.root / "deploy",
                deploy_branch="pi/app-demo",
                trunk_branch="pi/trunk",
                docs_dir="docs"),
            self.api, log=lambda _m: None)

        hook(Task(id="pizza_fan_site", body="b", meta={"demo": True}),
             self.repo)

        tree = self.origin_tree("pi/app-demo")
        self.assertIn("README.md", tree)  # trunk content came along
        docs = [p for p in tree if p.startswith("docs/")]
        self.assertEqual(docs, ["docs/index.html"])  # artifacts only
        blob = _git(self.origin, "cat-file", "blob", "pi/app-demo:docs/index.html")
        self.assertIn(TITLE, blob)
        self.assertIn("is in flight", blob)
        self.assertEqual(
            self.api.comments,
            [(7, "Placeholder deployed — https://acme.github.io/widgets/")])


# ---------------------------------------------------------------------------
# composition gating
# ---------------------------------------------------------------------------

class CompositionGatingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)

    def cfg(self, raw: dict) -> Config:
        cfg = _cfg(self.work_dir)
        cfg.raw = raw
        return cfg

    def test_demo_disabled_yields_no_hook_even_with_an_api(self):
        self.assertIsNone(build_placeholder_hook(self.cfg({}), api=FakeApi()))
        raw = {"demo": {"enabled": False, "githubPat": "t",
                        "githubRepo": REPO}}
        self.assertIsNone(build_placeholder_hook(self.cfg(raw),
                                                 api=FakeApi()))

    def test_github_unconfigured_yields_no_hook(self):
        raw = {"demo": {"enabled": True}}  # no pat/repo
        self.assertIsNone(build_placeholder_hook(self.cfg(raw)))

    def test_enabled_and_configured_yields_a_hook(self):
        raw = {"demo": {"enabled": True}}
        hook = build_placeholder_hook(self.cfg(raw), api=FakeApi())
        self.assertIsInstance(hook, DemoPlaceholderHook)
        self.assertEqual(hook.params.deploy_branch, "pi/app-demo")
        self.assertEqual(hook.params.apps_dir, "demo-apps")
        self.assertEqual(hook.params.docs_dir, "docs")
        self.assertEqual(hook.params.deploy_dir,
                         self.work_dir / "demo-deploy")


if __name__ == "__main__":
    unittest.main()
