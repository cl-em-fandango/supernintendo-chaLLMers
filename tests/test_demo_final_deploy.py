"""Slice 8 — active-app manifest use + final-deploy hook (FR-6.2, FR-7;
AC 3, 4, 7, 8).

Four layers, all in-process (spec §6):

  * the build runner (`demo_build`): a `package.json` app builds via a
    fake `npm` on PATH (cwd + invocations recorded), a bare `index.html`
    app needs no npm, an empty directory or a failing/missing npm is a
    clear `AppBuildError`;
  * the hook (`DemoFinalDeployHook`) with a fake deployer + fake API:
    the manifest-named app is the one handed to the builder even when
    two `demo-apps/*` directories exist (AC 4), the success comment
    `Deployed: <Pages URL>` lands on the fake issue (AC 7), and missing
    manifests / missing apps / deployer failures comment and return a
    reason instead of raising;
  * the pipeline hook site: `merge_to_trunk` -> final deploy ->
    `complete()` ordering asserted on one recorded call sequence; a
    failing deploy routes the task to `failed/`; a non-demo task never
    invokes the deployer (AC 8); a task parked before the final hook
    does not deploy (FR-6.4);
  * the real Slice 4 deployer + real build runner against a
    `git init --bare` fake origin: origin `pi/app-demo` descends from
    the refreshed local trunk, `docs/` holds only the built active app,
    `DEPLOYED.json` is on the deploy branch, fake npm ran exactly once
    for the manifest-named app, and origin `pi/trunk` is untouched
    (AC 3).

No real `pi`, no real npm, no network.

Run from the repo root:  python3 -m unittest tests.test_demo_final_deploy
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.demo_deploy import DemoDeployError, DeployStep
from harness.composition import build_final_deploy_hook
from harness.core import gitops
from harness.core.config import Config
from harness.core.enums import Verdict
from harness.core.providers import Task
from tests.legacy_sidecars import (
    SyncLinkage,
    task_dir_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.demo_build import (
    AppBuildError,
    build_active_app,
    build_plan_for,
)
from harness.workflow.demo_content import ContentSource, SiteContent
from harness.workflow.demo_final_deploy import (
    DemoFinalDeployHook,
    FinalDeployParams,
)
from harness.workflow.demo_generate import (
    DemoAppGenerationHook,
    DemoGenerationHookParams,
)
from harness.workflow.demo_manifest import (
    MANIFEST_NAME,
    ActiveAppManifest,
    read_manifest,
    write_manifest,
)
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline

REPO = "acme/widgets"
FAKE_NPM = """#!/bin/sh
echo "npm $* cwd=$PWD" >> "$NPM_RECORD"
if [ -n "$NPM_FAIL" ]; then echo "boom" >&2; exit 1; fi
if [ "$1" = "install" ]; then
  echo '{"lockfileVersion": 3}' > package-lock.json
fi
if [ "$1" = "run" ] && [ "$2" = "build" ]; then
  OUT="${NPM_OUT_DIR:-build}"
  mkdir -p "$OUT" && echo '<html>built active app</html>' > "$OUT/index.html"
fi
exit 0
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com",
         "-c", "user.name=Test", *args],
        cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


class _NpmTestBase(unittest.TestCase):
    """Temp dirs plus a controllable fake `npm` on PATH."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "npm-calls.txt"
        self._old_path = os.environ.get("PATH", "")
        for var in ("NPM_RECORD", "NPM_FAIL", "NPM_OUT_DIR"):
            os.environ.pop(var, None)
            self.addCleanup(os.environ.pop, var, None)
        os.environ["NPM_RECORD"] = str(self.record)
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)

    def install_fake_npm(self):
        npm = self.bin / "npm"
        npm.write_text(FAKE_NPM)
        npm.chmod(npm.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"

    def strip_npm(self):
        (self.root / "emptybin").mkdir(exist_ok=True)
        os.environ["PATH"] = str(self.root / "emptybin")

    def calls(self) -> list[str]:
        if not self.record.exists():
            return []
        return self.record.read_text().splitlines()


# ---------------------------------------------------------------------------
# layer A: the build runner
# ---------------------------------------------------------------------------

class BuildRunnerTest(_NpmTestBase):
    def node_app(self, name: str = "active-app") -> Path:
        app = self.root / "checkout" / "demo-apps" / name
        (app / "src").mkdir(parents=True)
        (app / "package.json").write_text(json.dumps(
            {"name": name, "scripts": {"build": "react-scripts build"}}),
            encoding="utf-8")
        (app / "src" / "index.js").write_text("// app\n", encoding="utf-8")
        return app

    def test_node_app_builds_via_npm_in_its_own_directory(self):
        self.install_fake_npm()
        app = self.node_app()
        artifacts = build_active_app(app)
        self.assertEqual(artifacts, app / "build")
        self.assertTrue((artifacts / "index.html").is_file())
        recorded = self.calls()
        self.assertEqual(len(recorded), 2)
        self.assertIn("npm install", recorded[0])
        self.assertIn("npm run build", recorded[1])
        for line in recorded:
            self.assertIn(f"cwd={app}", line)

    def vue_app(self, name: str = "vue-app") -> Path:
        """A vite project: the slice 7 scaffold shape (`dist/` output)."""
        app = self.root / "checkout-vue" / "demo-apps" / name
        app.mkdir(parents=True)
        (app / "package.json").write_text(json.dumps(
            {"name": name,
             "scripts": {"build": "vite build"},
             "devDependencies": {"vite": "^5.0.0"}}), encoding="utf-8")
        return app

    def test_vue_app_builds_its_declared_dist_directory(self):
        """FR-7.3: the artifact dir comes from the declared build tool,
        not a CRA constant — a vite app deploys its `dist/`."""
        self.install_fake_npm()
        os.environ["NPM_OUT_DIR"] = "dist"
        app = self.vue_app()
        self.assertEqual(build_plan_for(app).artifact_dir, "dist")
        artifacts = build_active_app(app)
        self.assertEqual(artifacts, app / "dist")
        self.assertTrue((artifacts / "index.html").is_file())

    def test_unknown_build_tool_is_a_clear_failure(self):
        app = self.root / "webpack-app"
        app.mkdir()
        (app / "package.json").write_text(json.dumps(
            {"name": "webpack-app",
             "scripts": {"build": "webpack --mode production"}}),
            encoding="utf-8")
        with self.assertRaises(AppBuildError) as caught:
            build_plan_for(app)
        self.assertIn("no known build tool", str(caught.exception))

    def test_unreadable_package_json_is_a_clear_failure(self):
        app = self.root / "broken-app"
        app.mkdir()
        (app / "package.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(AppBuildError) as caught:
            build_plan_for(app)
        self.assertIn("not readable JSON", str(caught.exception))

    def test_static_app_needs_no_npm(self):
        self.strip_npm()
        app = self.root / "static-app"
        app.mkdir()
        (app / "index.html").write_text("<html>hi</html>", encoding="utf-8")
        self.assertEqual(build_active_app(app), app.resolve())
        self.assertEqual(self.calls(), [])

    def test_empty_directory_declares_no_app(self):
        empty = self.root / "nothing"
        empty.mkdir()
        with self.assertRaises(AppBuildError):
            build_active_app(empty)
        with self.assertRaises(AppBuildError):
            build_plan_for(empty)

    def test_missing_npm_is_a_clear_failure(self):
        self.strip_npm()
        with self.assertRaises(AppBuildError) as caught:
            build_active_app(self.node_app())
        self.assertIn("npm unavailable", str(caught.exception))

    def test_failing_npm_reports_the_command(self):
        self.install_fake_npm()
        os.environ["NPM_FAIL"] = "1"
        with self.assertRaises(AppBuildError) as caught:
            build_active_app(self.node_app())
        self.assertIn("npm install failed", str(caught.exception))

    def test_build_without_artifacts_is_a_failure(self):
        self.install_fake_npm()
        npm = self.bin / "npm"
        npm.write_text("#!/bin/sh\nexit 0\n")
        npm.chmod(npm.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        with self.assertRaises(AppBuildError) as caught:
            build_active_app(self.node_app())
        self.assertIn("build/index.html", str(caught.exception))


# ---------------------------------------------------------------------------
# layer B: the hook with a fake deployer and fake API
# ---------------------------------------------------------------------------

class FakeApi:
    def __init__(self):
        self.comments: list[tuple[int, str]] = []

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
        self.built: list = []
        self.messages: list[str] = []

    def params(self) -> FinalDeployParams:
        return FinalDeployParams(
            queue_dir=self.queue_dir,
            apps_dir="demo-apps",
            harness_repo=self.root / "harness",
            deploy_dir=self.root / "deploy",
            deploy_branch="pi/app-demo",
            trunk_branch="pi/trunk",
            docs_dir="docs")

    def link(self, task_id: str = "pizza_fan_site", issue: int = 7,
             repo: str = REPO) -> None:
        task_dir = self.queue_dir / "active" / task_id
        task_dir.mkdir(parents=True)
        write_legacy_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=issue, repo=repo, demo=True))

    def ctx(self, task_id: str = "pizza_fan_site") -> StageContext:
        return StageContext(task_id=task_id,
                            task_dir=self.queue_dir / "active" / task_id,
                            workdir=self.workdir, demo=True)

    def active_app(self, app: str = "pizza-fan-site", issue: int = 7,
                   task: str = "pizza_fan_site") -> None:
        """The post-merge trunk shape: app source + manifest."""
        apps = self.workdir / "demo-apps"
        (apps / app / "src").mkdir(parents=True)
        (apps / app / "package.json").write_text('{"name": "a"}',
                                                 encoding="utf-8")
        write_manifest(apps, ActiveAppManifest(app=app, issue=issue,
                                               task=task))

    def hook(self, deployer=None, builder=None) -> DemoFinalDeployHook:
        def record_builder(app_dir, log=None):
            self.built.append(Path(app_dir))
            artifacts = Path(app_dir).parent / "_artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "index.html").write_text("<html>built</html>",
                                                  encoding="utf-8")
            return artifacts

        return DemoFinalDeployHook(
            self.params(), self.api,
            deployer=deployer or self.deployed.append,
            builder=builder or record_builder,
            origin_resolver=lambda repo: "https://origin.example/repo.git",
            log=self.messages.append)

    def test_success_builds_manifest_app_and_comments_the_pages_url(self):
        self.link()
        self.active_app()

        reason = self.hook()(self.ctx())

        self.assertEqual(reason, "")
        self.assertEqual(len(self.deployed), 1)
        request = self.deployed[0]
        self.assertEqual(request.deploy_branch, "pi/app-demo")
        self.assertEqual(request.docs_dir, "docs")
        self.assertEqual(request.trunk_branch, "pi/trunk")
        self.assertEqual(request.origin_url, "https://origin.example/repo.git")
        self.assertIsNone(request.artifacts_dir)
        self.assertEqual(
            self.api.comments,
            [(7, "Deployed: https://acme.github.io/widgets/")])

    def test_only_the_manifest_named_app_is_built(self):
        """AC 4: two app directories, the builder sees the manifest's."""
        self.link()
        self.active_app(app="active-app")
        older = self.workdir / "demo-apps" / "older-app"
        (older / "src").mkdir(parents=True)
        (older / "package.json").write_text('{"name": "old"}',
                                            encoding="utf-8")

        hook = self.hook()
        reason = hook(self.ctx())

        self.assertEqual(reason, "")
        # the builder closure the deployer received resolves the
        # manifest-named app inside whatever checkout it is handed
        checkout = self.root / "fake-checkout"
        request = self.deployed[0]
        built = request.builder(checkout)
        self.assertEqual(self.built[-1],
                         checkout / "demo-apps" / "active-app")
        self.assertTrue(Path(built).is_dir())

    def test_missing_manifest_fails_without_deploying(self):
        self.link()
        (self.workdir / "demo-apps").mkdir()

        reason = self.hook()(self.ctx())

        self.assertIn(MANIFEST_NAME, reason)
        self.assertEqual(self.deployed, [])
        self.assertEqual(len(self.api.comments), 1)
        self.assertTrue(
            self.api.comments[0][1].startswith("Demo deployment failed at "),
            self.api.comments[0][1])

    def test_manifest_naming_a_missing_app_fails(self):
        self.link()
        self.active_app(app="ghost-app")
        import shutil
        shutil.rmtree(self.workdir / "demo-apps" / "ghost-app")

        reason = self.hook()(self.ctx())

        self.assertIn("ghost-app", reason)
        self.assertEqual(self.deployed, [])

    def test_deployer_failure_comments_the_step_and_returns_the_reason(self):
        """FR-8.1: the comment names the failed step; the pipeline routes."""
        self.link()
        self.active_app()

        def failing(request):
            raise DemoDeployError(DeployStep.PUSH, "push rejected")

        reason = self.hook(deployer=failing)(self.ctx())

        self.assertIn("push rejected", reason)
        self.assertEqual(
            self.api.comments,
            [(7, "Demo deployment failed at push: push rejected")])

    def test_unlinked_task_deploys_and_comments_nothing(self):
        self.active_app()
        ctx = self.ctx(task_id="orphan_task")
        self.assertEqual(self.hook()(ctx), "")
        self.assertEqual(self.deployed, [])
        self.assertEqual(self.api.comments, [])


# ---------------------------------------------------------------------------
# layer C: the pipeline hook site (ordering, routing, inertness)
# ---------------------------------------------------------------------------

def _cfg(work_dir: Path, repo: Path | None = None) -> Config:
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
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "README.md").write_text("work target\n")
    _git(root, "init", "-b", "pi/trunk")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


class HolisticPipeline(Pipeline):
    """Stages before holistic are recorded no-ops; the holistic session
    returns a canned verdict; merge/complete/fail are recorded instead of
    performed."""

    def __init__(self, *args, holistic_verdict=Verdict.PASS, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []
        self.holistic_verdict = holistic_verdict

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

    def _run(self, model, workdir, prompt, **kw):
        return SimpleNamespace(verdict=self.holistic_verdict,
                               output="VERDICT: PASS", out_file=None)


class PipelineHookSiteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.repo = _make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        self.messages: list[str] = []
        # The merge and the branch cleanup are real git in these tests;
        # only their recording matters here.
        self._patches = []
        self._patch(gitops, "cleanup_branch",
                    lambda *a, **k: self.calls.append("cleanup"))
        self.calls: list[str] = []
        self._patch(gitops, "merge_to_trunk",
                    lambda *a, **k: self.calls.append("merge"))

    def _patch(self, module, name, replacement):
        original = getattr(module, name)
        self._patches.append((module, name, original))
        setattr(module, name, replacement)
        self.addCleanup(setattr, module, name, original)

    def pipeline(self, hook, verdict=Verdict.PASS):
        p = HolisticPipeline(self.cfg, runner=object(),
                             log=self.messages.append,
                             final_deploy_hook=hook,
                             holistic_verdict=verdict)
        p.calls = self.calls  # stage recordings land on the test's list
        self._patch(p.lifecycle, "complete",
                    lambda *a, **k: self.calls.append("complete"))
        self._patch(p.lifecycle, "fail",
                    lambda *a, **k: self.calls.append("fail"))
        return p

    def test_merge_then_final_deploy_then_complete(self):
        seen: list = []

        def hook(ctx):
            self.calls.append("final_deploy")
            seen.append(ctx)
            return ""

        pipeline = self.pipeline(hook)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "done")
        self.assertEqual(
            self.calls,
            ["spec", "feasibility", "slicing", "slices", "merge",
             "final_deploy", "complete", "cleanup"])
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].demo)
        self.assertEqual(seen[0].task_id, "demo_app")

    def test_non_demo_task_never_invokes_the_deployer(self):
        """AC 8: a non-demo task flows through the untouched merge path."""
        pipeline = self.pipeline(lambda ctx: self.calls.append("deploy") or "")
        outcome = pipeline.process(Task(id="plain_task", body="b"))
        self.assertEqual(outcome, "done")
        self.assertNotIn("deploy", self.calls)
        self.assertIn("merge", self.calls)
        self.assertIn("complete", self.calls)

    def test_final_deploy_failure_routes_to_failed(self):
        """FR-8.1: a failed Pages deployment fails the task, not the run,
        and the completion move never happens."""
        def hook(ctx):
            self.calls.append("final_deploy")
            return "push rejected"

        pipeline = self.pipeline(hook)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "failed")
        self.assertEqual(self.calls.count("final_deploy"), 1)
        self.assertIn("fail", self.calls)
        self.assertNotIn("complete", self.calls)

    def test_raising_hook_is_routed_not_crashed(self):
        def hook(ctx):
            raise RuntimeError("hook exploded")

        pipeline = self.pipeline(hook)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "failed")
        self.assertNotIn("complete", self.calls)

    def test_holistic_failure_parks_before_the_final_hook(self):
        """FR-6.4: a task that never reaches the post-merge hook does not
        deploy."""
        pipeline = self.pipeline(
            lambda ctx: self.calls.append("deploy") or "",
            verdict=Verdict.FAIL)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "parked")
        self.assertNotIn("deploy", self.calls)
        self.assertNotIn("merge", self.calls)

    def test_earlier_stage_park_does_not_deploy(self):
        pipeline = self.pipeline(
            lambda ctx: self.calls.append("deploy") or "")
        pipeline.stage_slices = lambda ctx: (self.calls.append("slices"),
                                             False)[1]
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "parked")
        self.assertNotIn("deploy", self.calls)

    def _run_once(self, pipeline, task_id="demo_app", demo=True):
        meta = {"demo": True} if demo else {}
        return pipeline.process(Task(id=task_id, body="b", meta=meta))

    def test_resume_after_merge_checkpoint_deploys_before_complete(self):
        """FR-6.2: a crash between the merge checkpoint and the deploy
        still deploys on resume — the `_is_merged` path runs the hook
        before `complete()` and never re-merges."""
        first = self.pipeline(
            lambda ctx: self.calls.append("final_deploy") or "")
        self.assertEqual(self._run_once(first), "done")
        self.assertIn("merge", self.calls)
        self.calls.clear()

        second = self.pipeline(
            lambda ctx: self.calls.append("final_deploy") or "")
        outcome = self._run_once(second)

        self.assertEqual(outcome, "done")
        self.assertNotIn("merge", self.calls)
        self.assertIn("final_deploy", self.calls)
        self.assertLess(self.calls.index("final_deploy"),
                        self.calls.index("complete"))

    def test_resume_deploy_failure_routes_to_failed(self):
        """FR-8.1 on the resume path: a failed deploy fails the task;
        the completion move never happens."""
        first = self.pipeline(lambda ctx: "")
        self.assertEqual(self._run_once(first), "done")
        self.calls.clear()

        def failing_hook(ctx):
            self.calls.append("final_deploy")
            return "push rejected"

        outcome = self._run_once(self.pipeline(failing_hook))

        self.assertEqual(outcome, "failed")
        self.assertIn("final_deploy", self.calls)
        self.assertIn("fail", self.calls)
        self.assertNotIn("complete", self.calls)

    def test_resume_non_demo_never_deploys(self):
        """FR-6.3 holds on the resume path too."""
        first = self.pipeline(None)
        self.assertEqual(self._run_once(first, task_id="plain_task",
                                        demo=False), "done")
        self.calls.clear()

        outcome = self._run_once(
            self.pipeline(
                lambda ctx: self.calls.append("deploy") or ""),
            task_id="plain_task", demo=False)

        self.assertEqual(outcome, "done")
        self.assertNotIn("deploy", self.calls)
        self.assertIn("complete", self.calls)

    def test_unwired_hook_leaves_the_holistic_path_untouched(self):
        pipeline = self.pipeline(None)
        outcome = pipeline.process(Task(id="demo_app", body="b",
                                        meta={"demo": True}))
        self.assertEqual(outcome, "done")
        self.assertIn("merge", self.calls)
        self.assertIn("complete", self.calls)


# ---------------------------------------------------------------------------
# layer D: the real deployer + real build against a fake origin
# ---------------------------------------------------------------------------

class RealDeployerTest(_NpmTestBase):
    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        _git(self.root, "init", "--bare", "-b", "pi/trunk", str(self.origin))
        self.repo = self.root / "harness"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("work target\n")
        _git(self.repo, "init", "-b", "pi/trunk")
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        self._seed_apps_on_trunk()
        self.queue_dir = self.root / "queue"
        task_dir = self.queue_dir / "active" / "pizza_fan_site"
        task_dir.mkdir(parents=True)
        write_legacy_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        self.api = FakeApi()

    def _seed_apps_on_trunk(self) -> None:
        """The post-merge trunk: two apps, the manifest names one."""
        apps = self.repo / "demo-apps"
        for name in ("active-app", "older-app"):
            (apps / name / "src").mkdir(parents=True)
            (apps / name / "package.json").write_text(
                json.dumps({"name": name,
                            "scripts": {"build": "react-scripts build"}}),
                encoding="utf-8")
            (apps / name / "src" / "index.js").write_text(
                f"// {name} source\n", encoding="utf-8")
        write_manifest(apps, ActiveAppManifest(app="active-app", issue=7,
                                               task="pizza_fan_site"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "demo apps merged to trunk")

    def hook(self) -> DemoFinalDeployHook:
        return DemoFinalDeployHook(
            FinalDeployParams(
                queue_dir=self.queue_dir,
                apps_dir="demo-apps",
                harness_repo=self.repo,
                deploy_dir=self.root / "deploy",
                deploy_branch="pi/app-demo",
                trunk_branch="pi/trunk",
                docs_dir="docs"),
            self.api, log=lambda _m: None)

    def ctx(self) -> StageContext:
        return StageContext(task_id="pizza_fan_site",
                            task_dir=self.queue_dir / "active"
                            / "pizza_fan_site",
                            workdir=self.repo, demo=True)

    def origin_tree(self, branch: str) -> list[str]:
        return sorted(_git(self.origin, "ls-tree", "-r", "--name-only",
                           branch).split())

    def test_final_deploy_publishes_only_the_active_app(self):
        self.install_fake_npm()
        trunk_sha = _git(self.repo, "rev-parse", "pi/trunk").strip()

        reason = self.hook()(self.ctx())

        self.assertEqual(reason, "")
        tree = self.origin_tree("pi/app-demo")
        # (a) descends from the refreshed local trunk (rebase happened)
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", trunk_sha, "pi/app-demo"],
            cwd=str(self.origin), capture_output=True, text=True)
        self.assertEqual(anc.returncode, 0, anc.stderr)
        # (b) docs/ holds only the built active app
        docs = [p for p in tree if p.startswith("docs/")]
        self.assertEqual(docs, ["docs/index.html"])
        blob = _git(self.origin, "cat-file", "blob", "pi/app-demo:docs/index.html")
        self.assertIn("built active app", blob)
        # sources and the other app are on the branch as history, not in docs
        self.assertIn("demo-apps/active-app/src/index.js", tree)
        self.assertIn("demo-apps/older-app/src/index.js", tree)
        # DEPLOYED.json is on the deploy branch (it rode in on trunk)
        self.assertIn(f"demo-apps/{MANIFEST_NAME}", tree)
        # (c) fake npm ran once, only for the manifest-named app
        builds = [line for line in self.calls() if "run build" in line]
        self.assertEqual(len(builds), 1)
        for line in self.calls():
            self.assertIn("demo-apps/active-app", line)
            self.assertNotIn("older-app", line)
        # (d) origin pi/trunk was never pushed by the deployer
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "pi/trunk"],
            cwd=str(self.origin), capture_output=True, text=True)
        self.assertNotEqual(probe.returncode, 0)
        # FR-8.2 success comment with the derived Pages URL
        self.assertEqual(
            self.api.comments,
            [(7, "Deployed: https://acme.github.io/widgets/")])

    def test_repeat_deploy_in_same_checkout_succeeds(self):
        """FR-8.4 / edge case 3: two consecutive hook invocations in the
        same deployDir. The build rewrites the trunk-tracked
        `package-lock.json` inside the checkout; without a reset/clean
        the second deploy dies at checkout/rebase."""
        self.install_fake_npm()
        lock = self.repo / "demo-apps/active-app/package-lock.json"
        lock.write_text('{"lockfileVersion": 2, "stale": true}\n',
                        encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "lock file on trunk")

        first = self.hook()(self.ctx())
        second = self.hook()(self.ctx())

        self.assertEqual(first, "")
        self.assertEqual(second, "")
        self.assertEqual(self.api.comments,
                         [(7, "Deployed: https://acme.github.io/widgets/"),
                          (7, "Deployed: https://acme.github.io/widgets/")])
        tree = self.origin_tree("pi/app-demo")
        self.assertEqual([p for p in tree if p.startswith("docs/")],
                         ["docs/index.html"])
        # build residue is never committed; trunk's lock file rides along
        self.assertNotIn("demo-apps/active-app/build/index.html", tree)
        self.assertIn("demo-apps/active-app/package-lock.json", tree)

    def test_failing_build_leaves_origin_without_a_deploy_branch(self):
        """FR-8.1 pre-push half: a build failure never reaches origin."""
        self.install_fake_npm()
        os.environ["NPM_FAIL"] = "1"

        reason = self.hook()(self.ctx())

        self.assertIn("npm install failed", reason)
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "pi/app-demo"],
            cwd=str(self.origin), capture_output=True, text=True)
        self.assertNotEqual(probe.returncode, 0)
        self.assertEqual(
            self.api.comments,
            [(7, "Demo deployment failed at build: " + reason)])


# ---------------------------------------------------------------------------
# the generation hook records the active app (manifest write)
# ---------------------------------------------------------------------------

class GenerationHookManifestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.queue_dir = self.root / "queue"
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()
        task_dir = self.queue_dir / "active" / "pizza_fan_site"
        task_dir.mkdir(parents=True)
        (task_dir / "original.md").write_text("make a pizza site\n",
                                              encoding="utf-8")
        write_legacy_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=7, repo=REPO, demo=True))

    def hook(self, generator) -> DemoAppGenerationHook:
        class Api:
            def get_issue(self, number):
                return SimpleNamespace(title="Pizza Fan Site")

        return DemoAppGenerationHook(
            DemoGenerationHookParams(
                queue_dir=self.queue_dir,
                apps_dir="demo-apps",
                repo=REPO,
                content_model="m",
                fallback_topic="Morris Dancing",
                app_model="m",
                output_dir=self.root / "out"),
            Api(),
            content_generator=lambda params, request, **kw: SiteContent(
                payload={"title": "T"}, source=ContentSource.FALLBACK),
            generator=generator,
            log=lambda _m: None)

    def ctx(self) -> StageContext:
        return StageContext(task_id="pizza_fan_site",
                            task_dir=self.queue_dir / "active"
                            / "pizza_fan_site",
                            workdir=self.workdir, demo=True)

    def test_generation_records_the_active_app_beside_the_source(self):
        def generator(params, request, workdir, **kw):
            app = Path(workdir) / params.apps_dir / request.app_name
            app.mkdir(parents=True)
            return SimpleNamespace(app_dir=app, built=True, reason="")

        self.hook(generator)(self.ctx())

        manifest = read_manifest(self.workdir / "demo-apps")
        self.assertEqual(manifest,
                         ActiveAppManifest(app="pizza-fan-site", issue=7,
                                           task="pizza_fan_site"))

    def test_a_failed_build_still_marks_the_app_active(self):
        """The implementer may still fix the build; the final deploy
        rebuilds from trunk either way."""
        def generator(params, request, workdir, **kw):
            return SimpleNamespace(app_dir=self.workdir, built=False,
                                   reason="npm unavailable")

        self.hook(generator)(self.ctx())

        self.assertEqual(read_manifest(self.workdir / "demo-apps").app,
                         "pizza-fan-site")


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
        self.assertIsNone(
            build_final_deploy_hook(self.cfg({}), api=FakeApi()))

    def test_github_unconfigured_yields_no_hook(self):
        self.assertIsNone(
            build_final_deploy_hook(self.cfg({"demo": {"enabled": True}})))

    def test_enabled_and_configured_yields_a_hook(self):
        hook = build_final_deploy_hook(self.cfg({"demo": {"enabled": True}}),
                                       api=FakeApi())
        self.assertIsInstance(hook, DemoFinalDeployHook)
        self.assertEqual(hook.params.deploy_branch, "pi/app-demo")
        self.assertEqual(hook.params.apps_dir, "demo-apps")
        self.assertEqual(hook.params.docs_dir, "docs")
        self.assertEqual(hook.params.deploy_dir,
                         self.work_dir / "demo-deploy")


if __name__ == "__main__":
    unittest.main()
