"""Slice 9 — failure handling and observability (demo FR-8; AC 6, AC 7).

Five layers, all in-process (spec §6): no real `pi`, no real npm, no
network, temp git repos with `git init --bare` fake origins.

  * generation-step failures (`DemoAppGenerationHook`): a failing content
    generator or a failing scaffold comments
    `Demo deployment failed at content|scaffold: <reason>` on the issue,
    writes no manifest, and re-raises so the pipeline's guard routes;
  * the real deployer against a fake origin: a failing `npm` build, a
    rebase conflict outside `docs/`, and a rejected push each leave the
    previous `docs/` byte-identical on origin (FR-8.1, no half-written
    commit) and produce the step-named comment through the final-deploy
    hook;
  * the pipeline hook site with the *real* merge: `npm` exit 1 during
    the final deploy ends the task in `failed/`, the merged trunk source
    stands (FR-6.4), and nothing reaches origin's deploy branch;
  * placeholder failures: the hook comments and never raises, and even a
    hook that breaks its contract cannot cost the task its spec work
    (FR-2.3);
  * FR-8.3 logging: every deploy step logs through the caller's sink —
    `publish_artifacts` on its own, and the hooks handing their log to
    the deploy request.
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

import external.git_cli as git_cli
from external.demo_deploy import (
    DemoDeployError,
    DeployStep,
    publish_artifacts,
)
from harness.core.config import Config
from harness.core.enums import Verdict
from harness.core.providers import Task
from harness.core.sync_sidecar import (
    SyncLinkage,
    task_dir_sidecar_path,
    write_linkage,
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
    write_manifest,
)
from harness.workflow.demo_placeholder import (
    DemoPlaceholderHook,
    PlaceholderDeployParams,
)
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline

REPO = "acme/widgets"
PAGES_URL = "https://acme.github.io/widgets/"

FAKE_NPM = """#!/bin/sh
echo "npm $* cwd=$PWD" >> "$NPM_RECORD"
if [ -n "$NPM_FAIL" ]; then echo "boom" >&2; exit 1; fi
if [ "$1" = "install" ]; then
  echo '{"lockfileVersion": 3}' > package-lock.json
fi
if [ "$1" = "run" ] && [ "$2" = "build" ]; then
  mkdir -p build && echo '<html>built active app</html>' > build/index.html
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


def _ref_exists(repo: Path, ref: str) -> bool:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(repo), capture_output=True, text=True)
    return probe.returncode == 0


class FakeApi:
    def __init__(self, title: str = "Pizza Fan Site"):
        self.comments: list[tuple[int, str]] = []
        self.title = title

    def create_comment(self, number, body):
        self.comments.append((number, body))

    def get_issue(self, number):
        return SimpleNamespace(title=self.title)


# ---------------------------------------------------------------------------
# layer A: generation-step failures comment the step and re-raise
# ---------------------------------------------------------------------------

class GenerationStepFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.queue_dir = self.root / "queue"
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()
        self.task_dir = self.queue_dir / "active" / "pizza_fan_site"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "original.md").write_text("make a pizza site\n",
                                                   encoding="utf-8")
        write_linkage(task_dir_sidecar_path(self.task_dir),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        self.api = FakeApi()
        self.messages: list[str] = []

    def hook(self, generator=None, content_generator=None, api=None):
        def ok_generator(params, request, workdir, **kw):
            app = Path(workdir) / params.apps_dir / request.app_name
            app.mkdir(parents=True)
            return SimpleNamespace(app_dir=app, built=True, reason="")

        return DemoAppGenerationHook(
            DemoGenerationHookParams(
                queue_dir=self.queue_dir,
                apps_dir="demo-apps",
                repo=REPO,
                content_model="m",
                fallback_topic="Morris Dancing",
                app_model="m",
                output_dir=self.root / "out"),
            api or self.api,
            content_generator=content_generator or (
                lambda params, request, **kw: SiteContent(
                    payload={"title": "T"}, source=ContentSource.FALLBACK)),
            generator=generator or ok_generator,
            log=self.messages.append)

    def ctx(self) -> StageContext:
        return StageContext(task_id="pizza_fan_site",
                            task_dir=self.task_dir,
                            workdir=self.workdir, demo=True)

    def test_content_failure_comments_the_content_step(self):
        def failing_content(params, request, **kw):
            raise RuntimeError("content model unavailable")

        with self.assertRaises(RuntimeError):
            self.hook(content_generator=failing_content)(self.ctx())

        self.assertEqual(
            self.api.comments,
            [(7, "Demo deployment failed at content: "
                 "content model unavailable")])
        self.assertFalse((self.workdir / "demo-apps" / MANIFEST_NAME).exists())

    def test_scaffold_failure_comments_the_scaffold_step(self):
        def failing_generator(params, request, workdir, **kw):
            raise RuntimeError("scaffold exploded")

        with self.assertRaises(RuntimeError):
            self.hook(generator=failing_generator)(self.ctx())

        self.assertEqual(
            self.api.comments,
            [(7, "Demo deployment failed at scaffold: scaffold exploded")])
        self.assertFalse((self.workdir / "demo-apps" / MANIFEST_NAME).exists())

    def test_a_failing_comment_never_replaces_the_original_failure(self):
        class ExplodingApi(FakeApi):
            def create_comment(self, number, body):
                raise RuntimeError("github down")

        def failing_generator(params, request, workdir, **kw):
            raise RuntimeError("scaffold exploded")

        with self.assertRaises(RuntimeError) as caught:
            self.hook(generator=failing_generator,
                      api=ExplodingApi())(self.ctx())
        self.assertIn("scaffold exploded", str(caught.exception))


# ---------------------------------------------------------------------------
# layers B–D: the real deployer + real hook against a fake origin
# ---------------------------------------------------------------------------

class RealOriginFailureBase(unittest.TestCase):
    """One successful deploy, then one injected failure per test.

    The invariant under test is FR-8.1's first bullet: whatever fails,
    origin's `docs/` after the failed attempt is byte-identical to the
    last good deployment and the deploy-branch head has not moved.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "npm-calls.txt"
        self._old_path = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)
        for var in ("NPM_RECORD", "NPM_FAIL"):
            os.environ.pop(var, None)
            self.addCleanup(os.environ.pop, var, None)
        os.environ["NPM_RECORD"] = str(self.record)

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
        write_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        self.api = FakeApi()
        self.messages: list[str] = []

    def _seed_apps_on_trunk(self) -> None:
        apps = self.repo / "demo-apps"
        (apps / "active-app" / "src").mkdir(parents=True)
        (apps / "active-app" / "package.json").write_text(
            json.dumps({"name": "active-app",
                        "scripts": {"build": "react-scripts build"}}),
            encoding="utf-8")
        (apps / "active-app" / "src" / "index.js").write_text(
            "// active source\n", encoding="utf-8")
        write_manifest(apps, ActiveAppManifest(app="active-app", issue=7,
                                               task="pizza_fan_site"))
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "demo apps merged to trunk")

    def install_fake_npm(self):
        npm = self.bin / "npm"
        npm.write_text(FAKE_NPM)
        npm.chmod(npm.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"

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
            self.api, log=self.messages.append)

    def ctx(self) -> StageContext:
        return StageContext(task_id="pizza_fan_site",
                            task_dir=self.queue_dir / "active"
                            / "pizza_fan_site",
                            workdir=self.repo, demo=True)

    def deploy_ok(self) -> None:
        """The last good deployment: origin holds `docs/index.html`."""
        self.install_fake_npm()
        reason = self.hook()(self.ctx())
        self.assertEqual(reason, "", "\n".join(self.messages))
        self.api.comments.clear()
        self.head = _git(self.origin, "rev-parse", "pi/app-demo").strip()
        self.docs = _git(self.origin, "cat-file", "blob",
                         "pi/app-demo:docs/index.html")

    def assert_docs_intact(self) -> None:
        self.assertEqual(
            _git(self.origin, "rev-parse", "pi/app-demo").strip(), self.head,
            "the failed deploy moved the deploy-branch head")
        self.assertEqual(
            _git(self.origin, "cat-file", "blob",
                 "pi/app-demo:docs/index.html"), self.docs,
            "the failed deploy changed docs/ — FR-8.1 requires the "
            "previous deployment to stand byte-identical")


class BuildFailureTest(RealOriginFailureBase):
    def test_failing_npm_keeps_the_previous_docs_and_comments_build(self):
        """FR-8.1/AC 6: `npm` exit 1 during the final deploy — the
        previous `docs/` stands byte-identical and the comment names the
        build step."""
        self.deploy_ok()
        os.environ["NPM_FAIL"] = "1"

        reason = self.hook()(self.ctx())

        self.assertIn("npm install failed", reason)
        self.assert_docs_intact()
        self.assertEqual(len(self.api.comments), 1)
        issue, comment = self.api.comments[0]
        self.assertEqual(issue, 7)
        self.assertTrue(comment.startswith(
            "Demo deployment failed at build: "), comment)


class RebaseFailureTest(RealOriginFailureBase):
    def test_conflict_outside_docs_keeps_previous_docs_and_comments_rebase(self):
        """FR-8.1: a deploy-branch/trunk conflict outside `docs/` aborts the
        rebase — nothing new is committed or pushed, the comment names the
        rebase step."""
        self.deploy_ok()
        # Diverge: trunk edits the app source...
        (self.repo / "demo-apps/active-app/src/index.js").write_text(
            "// trunk rewrite\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "trunk edits the app")
        # ...and so does the deploy branch, differently.
        scratch = self.root / "scratch"
        scratch.mkdir()
        _git(scratch, "clone", str(self.origin), "clone")
        clone = scratch / "clone"
        _git(clone, "checkout", "-b", "pi/app-demo", "origin/pi/app-demo")
        (clone / "demo-apps/active-app/src/index.js").write_text(
            "// deploy-branch rewrite\n", encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "deploy branch edits the same file")
        _git(clone, "push", "origin", "pi/app-demo")
        # The divergence push itself moved the head; from here on the
        # failed deploy must not move it or touch docs/ any further.
        self.head = _git(self.origin, "rev-parse", "pi/app-demo").strip()

        reason = self.hook()(self.ctx())

        self.assertIn("conflict outside the docs directory", reason)
        self.assert_docs_intact()
        self.assertEqual(len(self.api.comments), 1)
        issue, comment = self.api.comments[0]
        self.assertEqual(issue, 7)
        self.assertTrue(comment.startswith(
            "Demo deployment failed at rebase: "), comment)


class PushFailureTest(RealOriginFailureBase):
    def test_rejected_push_keeps_previous_docs_and_comments_push(self):
        """FR-8.1: a push the origin rejects leaves origin exactly as it
        was (the commit lives only in the local deploy checkout) and the
        comment names the push step."""
        self.deploy_ok()
        # A new trunk commit forces a real (non-empty) push attempt...
        (self.repo / "README.md").write_text("advanced\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "trunk advances")
        # ...and a read-only origin rejects it.
        for path in sorted(self.origin.rglob("*"), reverse=True):
            path.chmod(path.stat().st_mode & ~0o222)
        self.origin.chmod(self.origin.stat().st_mode & ~0o222)

        def restore() -> None:
            for path in sorted(self.origin.rglob("*")):
                path.chmod(path.stat().st_mode | 0o600)
            self.origin.chmod(self.origin.stat().st_mode | 0o700)

        self.addCleanup(restore)

        reason = self.hook()(self.ctx())

        self.assertIn("push of pi/app-demo failed", reason)
        self.assert_docs_intact()
        self.assertEqual(len(self.api.comments), 1)
        issue, comment = self.api.comments[0]
        self.assertEqual(issue, 7)
        self.assertTrue(comment.startswith(
            "Demo deployment failed at push: "), comment)


# ---------------------------------------------------------------------------
# layer E: the pipeline hook site, real merge, real deployer, failing npm
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


class FinalDeployRoutesToFailedTest(unittest.TestCase):
    """AC 6 end-to-end at the pipeline level: fake `npm` exit 1 during
    the final deploy ends the task in `failed/`, the merged trunk source
    is not rolled back (FR-6.4), and origin never sees a deploy branch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        # The merge itself (checkout, squash, commit) is what this test
        # exercises; the harness verification gate is judged elsewhere and
        # cannot meaningfully judge a fixture repo.
        original = git_cli.verify_harness
        git_cli.verify_harness = lambda workdir: (True, "fixture gate passes")
        self.addCleanup(setattr, git_cli, "verify_harness", original)
        self.origin = self.work_dir / "origin.git"
        _git(self.work_dir, "init", "--bare", "-b", "pi/trunk", str(self.origin))
        self.repo = self.work_dir / "repo"
        self.repo.mkdir()
        # gate_applies() only merges a repo shaped like the harness.
        (self.repo / "README.md").write_text("target repo\n")
        (self.repo / "harness.py").write_text("# gate marker\n")
        (self.repo / "harness").mkdir()
        (self.repo / "harness" / "composition.py").write_text("# gate marker\n")
        _git(self.repo, "init", "-b", "pi/trunk")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        self.cfg = _cfg(self.work_dir, repo=self.repo)
        self.api = FakeApi()
        self.messages: list[str] = []
        self.bin = self.work_dir / "bin"
        self.bin.mkdir()
        npm = self.bin / "npm"
        npm.write_text(FAKE_NPM)
        npm.chmod(npm.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)
        os.environ["NPM_RECORD"] = str(self.work_dir / "npm-calls.txt")
        self.addCleanup(os.environ.pop, "NPM_RECORD", None)
        os.environ["NPM_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "NPM_FAIL", None)

    def pipeline(self) -> Pipeline:
        outer = self

        class DemoPipeline(Pipeline):
            """Spec stages are stubs; the implement stub commits the app
            the way the implementer would; holistic + merge + final
            deploy are the real code paths."""

            def stage_spec(self, ctx):
                return True

            def stage_feasibility(self, ctx):
                return True

            def stage_slicing(self, ctx):
                return True

            def stage_slices(self, ctx):
                apps = Path(ctx.workdir) / "demo-apps"
                (apps / "pizza-fan-site" / "src").mkdir(parents=True)
                (apps / "pizza-fan-site" / "package.json").write_text(
                    json.dumps({"name": "pizza-fan-site",
                                "scripts": {"build": "react-scripts build"}}),
                    encoding="utf-8")
                (apps / "pizza-fan-site" / "src" / "index.js").write_text(
                    "// pizza\n", encoding="utf-8")
                write_manifest(apps, ActiveAppManifest(
                    app="pizza-fan-site", issue=7, task=ctx.task_id))
                _git(ctx.workdir, "add", "-A")
                _git(ctx.workdir, "commit", "-m", "demo app implemented")
                # intake has moved the task to active/ by now; link it.
                write_linkage(
                    task_dir_sidecar_path(outer.cfg.queue_dir / "active"
                                          / ctx.task_id),
                    SyncLinkage(issue=7, repo=REPO, demo=True))
                return True

            def _run(self, model, workdir, prompt, **kw):
                return SimpleNamespace(verdict=Verdict.PASS,
                                       output="VERDICT: PASS", out_file=None)

        hook = DemoFinalDeployHook(
            FinalDeployParams(
                queue_dir=self.cfg.queue_dir,
                apps_dir="demo-apps",
                harness_repo=self.repo,
                deploy_dir=self.work_dir / "deploy",
                deploy_branch="pi/app-demo",
                trunk_branch="pi/trunk",
                docs_dir="docs"),
            self.api, log=self.messages.append)
        return DemoPipeline(self.cfg, runner=object(),
                            log=self.messages.append, final_deploy_hook=hook)

    def test_build_failure_fails_the_task_and_keeps_trunk_source(self):
        outcome = self.pipeline().process(
            Task(id="pizza_fan_site", body="b", meta={"demo": True}))

        self.assertEqual(outcome, "failed", "\n".join(self.messages))
        self.assertTrue((self.cfg.queue_dir / "failed"
                         / "pizza_fan_site").is_dir())
        self.assertFalse((self.cfg.queue_dir / "done"
                          / "pizza_fan_site").exists())
        # FR-6.4: the merged trunk source stands — nothing was rolled back.
        trunk_files = _git(self.repo, "ls-tree", "-r", "--name-only",
                           "pi/trunk").split()
        self.assertIn("demo-apps/pizza-fan-site/src/index.js", trunk_files)
        self.assertIn(f"demo-apps/{MANIFEST_NAME}", trunk_files)
        # The failed deploy never reached origin.
        self.assertFalse(_ref_exists(self.origin, "pi/app-demo"))
        # FR-8.1 comment, naming the build step.
        self.assertEqual(len(self.api.comments), 1)
        issue, comment = self.api.comments[0]
        self.assertEqual(issue, 7)
        self.assertTrue(comment.startswith(
            "Demo deployment failed at build: "), comment)


# ---------------------------------------------------------------------------
# layer F: placeholder failures never cost the spec work (FR-2.3)
# ---------------------------------------------------------------------------

class PlaceholderFailureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.api = FakeApi()
        self.messages: list[str] = []

    def params(self) -> PlaceholderDeployParams:
        return PlaceholderDeployParams(
            queue_dir=self.queue_dir,
            apps_dir="demo-apps",
            harness_repo=self.work_dir / "repo",
            deploy_dir=self.work_dir / "deploy",
            deploy_branch="pi/app-demo",
            trunk_branch="pi/trunk",
            docs_dir="docs")

    def link(self, task_id: str = "pizza_fan_site", issue: int = 7) -> None:
        task_dir = self.queue_dir / "active" / task_id
        task_dir.mkdir(parents=True)
        write_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=issue, repo=REPO, demo=True))

    def test_deployer_failure_comments_and_never_raises(self):
        """FR-2.3: a failed placeholder deploy comments on the issue and
        the hook returns normally."""
        self.link()

        def failing(request):
            raise DemoDeployError(DeployStep.PUSH, "push rejected")

        hook = DemoPlaceholderHook(self.params(), self.api,
                                   deployer=failing,
                                   origin_resolver=lambda repo: "x",
                                   log=self.messages.append)
        hook(SimpleNamespace(id="pizza_fan_site", meta={"demo": True}),
             self.work_dir / "workdir")

        self.assertEqual(
            self.api.comments,
            [(7, "placeholder deployment failed: push: push rejected")])

    def test_raising_hook_still_completes_the_spec_stages(self):
        """FR-2.3 second line: even a hook that breaks its never-raise
        contract cannot stop the pipeline from running spec onward."""
        calls: list[str] = []

        def exploding_hook(task, workdir):
            calls.append("placeholder")
            raise RuntimeError("hook exploded")

        class StubPipeline(Pipeline):
            def stage_spec(self, ctx):
                calls.append("spec")
                return True

            def stage_feasibility(self, ctx):
                calls.append("feasibility")
                return True

            def stage_slicing(self, ctx):
                calls.append("slicing")
                return True

            def stage_slices(self, ctx):
                calls.append("slices")
                return True

            def stage_holistic(self, ctx):
                calls.append("holistic")
                return "done"

        repo = self.work_dir / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "pi/trunk")
        (repo / "README.md").write_text("target repo\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "init")
        cfg = _cfg(self.work_dir, repo=repo)
        pipeline = StubPipeline(cfg, runner=object(),
                                log=self.messages.append,
                                placeholder_hook=exploding_hook)
        outcome = pipeline.process(Task(id="pizza_fan_site", body="b",
                                        meta={"demo": True}))

        self.assertEqual(outcome, "done", "\n".join(self.messages))
        self.assertEqual(calls, ["placeholder", "spec", "feasibility",
                                 "slicing", "slices", "holistic"])


# ---------------------------------------------------------------------------
# layer G: FR-8.3 — deploy steps log through the caller's sink
# ---------------------------------------------------------------------------

class DeployLoggingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.origin = self.root / "origin.git"
        _git(self.root, "init", "--bare", "-b", "pi/trunk", str(self.origin))
        self.repo = self.root / "harness"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("work target\n")
        _git(self.repo, "init", "-b", "pi/trunk")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        (self.artifacts / "index.html").write_text("<html>static</html>",
                                                   encoding="utf-8")

    def test_publish_artifacts_logs_every_step_through_the_sink(self):
        lines: list[str] = []
        publish_artifacts(SimpleNamespace(
            origin_url=str(self.origin),
            harness_repo=self.repo,
            deploy_dir=self.root / "deploy",
            deploy_branch="pi/app-demo",
            trunk_branch="pi/trunk",
            docs_dir="docs",
            artifacts_dir=self.artifacts,
            builder=None,
            log=lines.append))

        joined = "\n".join(lines)
        for fragment in ("lock held", "rebased pi/app-demo onto pi/trunk",
                         "artifacts ready", "replaced with the active",
                         "committed", "pushed pi/app-demo to origin"):
            self.assertIn(fragment, joined, lines)

    def test_final_deploy_hook_hands_its_log_to_the_deploy_request(self):
        queue_dir = self.root / "queue"
        task_dir = queue_dir / "active" / "t1"
        task_dir.mkdir(parents=True)
        write_linkage(task_dir_sidecar_path(task_dir),
                      SyncLinkage(issue=1, repo=REPO, demo=True))
        apps = self.root / "workdir" / "demo-apps"
        (apps / "app").mkdir(parents=True)
        (apps / "app" / "index.html").write_text("<html>x</html>",
                                                 encoding="utf-8")
        write_manifest(apps, ActiveAppManifest(app="app", issue=1, task="t1"))
        requests: list = []
        messages: list[str] = []
        hook = DemoFinalDeployHook(
            FinalDeployParams(
                queue_dir=queue_dir, apps_dir="demo-apps",
                harness_repo=self.repo, deploy_dir=self.root / "deploy",
                deploy_branch="pi/app-demo", trunk_branch="pi/trunk",
                docs_dir="docs"),
            FakeApi(), deployer=requests.append,
            builder=lambda app_dir, log=None: Path(app_dir),
            origin_resolver=lambda repo: str(self.origin),
            log=messages.append)
        hook(StageContext(task_id="t1", task_dir=task_dir,
                          workdir=self.root / "workdir", demo=True))

        self.assertEqual(len(requests), 1)
        sink_lines: list[str] = []
        requests[0].log("sink reached")
        self.assertIn("sink reached", messages)


if __name__ == "__main__":
    unittest.main()
