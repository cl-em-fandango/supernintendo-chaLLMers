"""Slice 7.2/7.3 — generation driver, demo implementer variant, pipeline branch.

Layers, all in-process (spec §6):

  * the driver (`generate_demo_app`): scaffold + fake generation session
    + fake `npm` on PATH (records its cwd and invocations, produces
    `build/index.html`); npm missing -> "npm unavailable" with the stack
    unchanged; npm failing or producing nothing -> a clear failure reason;
    the plain-HTML stack builds without npm at all;
  * the prompts: the demo variant is the normal implement prompt plus
    the demo appendix, byte-identical base; the non-demo prompt is
    untouched;
  * the pipeline branch on `ctx.demo`: generator fires once for a demo
    task only, the demo prompt variant reaches demo tasks only, and a
    raising generator cannot stop the implement session;
  * the composition hook (`DemoAppGenerationHook`) with a fake api and
    injected content generator/driver, plus factory gating.

No real `pi`, no real npm, no network, no git.

Run from the repo root:
    python3 -m unittest tests.test_demo_implementer_variant
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.composition import build_demo_app_generator
from harness.core import prompts
from harness.core.config import Config
from harness.core.enums import Verdict
from tests.legacy_sidecars import (
    SyncLinkage,
    task_dir_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.demo_content import ContentSource, SiteContent
from harness.workflow.demo_placeholder import write_placeholder_app
from harness.workflow.demo_generate import (
    DemoAppGenerationHook,
    DemoGenerationHookParams,
    DemoGenerationParams,
    DemoGenerationRequest,
    generate_demo_app,
)
from harness.workflow.demo_stack import WebStack
from harness.workflow.params import StageContext
from harness.workflow.pipeline import Pipeline

FAKE_NPM = """#!/bin/sh
echo "npm $* cwd=$PWD" >> "$NPM_RECORD"
if [ -n "$NPM_FAIL" ]; then echo "boom" >&2; exit 1; fi
if [ "$1" = "run" ] && [ "$2" = "build" ] && [ -z "$NPM_NO_ARTIFACTS" ]; then
  mkdir -p build && echo '<html>built</html>' > build/index.html
fi
exit 0
"""


def _content() -> SiteContent:
    return SiteContent(payload={"title": "T", "sections": []},
                       source=ContentSource.FALLBACK)


def _ok_session(**kwargs):
    return SimpleNamespace(rc=0, crashed=False, output="")


class _DriverTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.workdir = self.root / "workdir"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "npm-calls.txt"
        self._old_path = os.environ.get("PATH", "")
        for var in ("NPM_RECORD", "NPM_FAIL", "NPM_NO_ARTIFACTS"):
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
        # A PATH with no npm anywhere: the bare empty bin dir only, so
        # the machine's real npm can never answer.
        (self.root / "emptybin").mkdir(exist_ok=True)
        os.environ["PATH"] = str(self.root / "emptybin")

    def params(self) -> DemoGenerationParams:
        return DemoGenerationParams(apps_dir="demo-apps",
                                    repo="acme/widgets",
                                    app_model="m",
                                    output_dir=self.root / "out")

    def request(self, body="a pizza site", title="Pizza Fan Site"):
        return DemoGenerationRequest(title=title, body=body,
                                     app_name="pizza-fan-site",
                                     content=_content())

    def calls(self) -> list[str]:
        if not self.record.exists():
            return []
        return self.record.read_text().splitlines()


class DriverTest(_DriverTestBase):
    def test_default_stack_builds_via_fake_npm(self):
        self.install_fake_npm()
        outcome = generate_demo_app(
            self.params(), self.request(), self.workdir,
            session_runner=_ok_session)
        self.assertTrue(outcome.built, outcome.reason)
        self.assertEqual(outcome.plan.stack, WebStack.CRA_MUI)
        app_dir = self.workdir / "demo-apps" / "pizza-fan-site"
        self.assertEqual(outcome.app_dir, app_dir)
        recorded = self.calls()
        self.assertEqual(len(recorded), 2)
        self.assertTrue(all(f"cwd={app_dir}" in line for line in recorded))
        self.assertIn("npm install", recorded[0])
        self.assertIn("npm run build", recorded[1])

    def test_content_and_scaffold_land_in_the_app_dir(self):
        self.install_fake_npm()
        generate_demo_app(self.params(), self.request(), self.workdir,
                          session_runner=_ok_session)
        app_dir = self.workdir / "demo-apps" / "pizza-fan-site"
        written = json.loads((app_dir / "src" / "content.json")
                             .read_text())
        self.assertEqual(written, {"title": "T", "sections": []})
        self.assertTrue((app_dir / "build" / "index.html").exists())

    def test_generation_session_runs_and_can_write_files(self):
        self.install_fake_npm()
        seen: list[dict] = []

        def fake_pi(*, model, workdir, prompt, out_file, log):
            seen.append({"model": model, "workdir": workdir,
                         "prompt": prompt})
            (Path(workdir) / "demo-apps" / "pizza-fan-site"
             / "src" / "Extra.js").write_text("// added by pi\n")
            return _ok_session()

        outcome = generate_demo_app(self.params(), self.request(),
                                    self.workdir, session_runner=fake_pi)
        self.assertTrue(outcome.built, outcome.reason)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["model"], "m")
        self.assertIn("pizza-fan-site", seen[0]["prompt"])
        self.assertTrue((self.workdir / "demo-apps" / "pizza-fan-site"
                         / "src" / "Extra.js").exists())

    def test_missing_npm_reports_unavailable_without_stack_swap(self):
        self.strip_npm()
        messages: list[str] = []
        outcome = generate_demo_app(
            self.params(), self.request(), self.workdir,
            session_runner=_ok_session, log=messages.append)
        self.assertFalse(outcome.built)
        self.assertIn("npm unavailable", outcome.reason)
        self.assertEqual(outcome.plan.stack, WebStack.CRA_MUI)
        self.assertEqual(self.calls(), [])

    def test_failing_npm_reports_the_failure(self):
        self.install_fake_npm()
        os.environ["NPM_FAIL"] = "1"
        outcome = generate_demo_app(self.params(), self.request(),
                                    self.workdir, session_runner=_ok_session)
        self.assertFalse(outcome.built)
        self.assertIn("npm install failed", outcome.reason)

    def test_build_without_artifacts_is_a_failure(self):
        self.install_fake_npm()
        os.environ["NPM_NO_ARTIFACTS"] = "1"
        outcome = generate_demo_app(self.params(), self.request(),
                                    self.workdir, session_runner=_ok_session)
        self.assertFalse(outcome.built)
        self.assertIn("build/index.html", outcome.reason)

    def test_plain_html_stack_needs_no_npm(self):
        self.strip_npm()
        outcome = generate_demo_app(
            self.params(), self.request(body="plain HTML only"),
            self.workdir, session_runner=_ok_session)
        self.assertTrue(outcome.built, outcome.reason)
        self.assertEqual(outcome.plan.stack, WebStack.PLAIN_HTML)

    def test_dead_generation_session_keeps_the_scaffold_build(self):
        self.install_fake_npm()

        def dead_pi(**kwargs):
            raise RuntimeError("pi died")

        outcome = generate_demo_app(self.params(), self.request(),
                                    self.workdir, session_runner=dead_pi)
        self.assertTrue(outcome.built, outcome.reason)


class PromptVariantTest(unittest.TestCase):
    def setUp(self):
        self.td = Path("/tmp/task-dir")

    def test_non_demo_prompt_is_untouched(self):
        base = prompts.implement_slice(self.td, "7", 1, 5)
        self.assertNotIn("DEMO WEB-APP TASK", base)
        demo = prompts.implement_slice_demo(self.td, "7", 1, 5)
        # the variant is the byte-identical base plus the appendix
        self.assertTrue(demo.startswith(base))
        self.assertIn("DEMO WEB-APP TASK", demo)
        self.assertIn("demo-apps", demo)

    def test_generation_prompt_names_app_dir_and_pages_path(self):
        text = prompts.demo_app_generation(Path("/tmp/demo-apps/pizza"),
                                           "cra-mui", "/widgets/")
        self.assertIn("/tmp/demo-apps/pizza", text)
        self.assertIn("/widgets/", text)


class FakeRunnerPipeline(Pipeline):
    """Records implement prompts instead of running sessions."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.implement_prompts: list[str] = []

    def _run(self, model, workdir, prompt, **kw):
        self.implement_prompts.append(prompt)
        return SimpleNamespace(verdict=Verdict.DONE)


class PipelineBranchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.cfg = Config(
            work_dir=self.work_dir, token_budget=100_000,
            max_spec_kickbacks=3, max_slice_implement=5,
            max_slice_tech_review=5, max_slice_func_review=5,
            max_slice_check_loops=3, autonomous_queue_target=5,
            trunk_branch="pi/trunk", task_provider="directory",
            directory_provider={},
            models={"technicalWriter": "m", "implementer": "m",
                    "assessor": "m"},
            model_context_map={}, repo_dir=self.work_dir / "repo")
        self.messages: list[str] = []

    def pipeline(self, generator):
        return FakeRunnerPipeline(self.cfg, runner=object(),
                                  log=self.messages.append,
                                  demo_app_generator=generator)

    def test_demo_task_gets_variant_and_one_generator_call(self):
        calls: list = []
        pipeline = self.pipeline(lambda ctx: calls.append(ctx))
        ctx = StageContext("t1", self.work_dir / "task", self.work_dir,
                           demo=True)
        self.assertTrue(pipeline._implement(ctx, "7"))
        self.assertTrue(pipeline._implement(ctx, "8"))
        self.assertEqual(calls, [ctx])  # generated once, not per slice
        self.assertEqual(len(pipeline.implement_prompts), 2)
        for prompt in pipeline.implement_prompts:
            self.assertIn("DEMO WEB-APP TASK", prompt)

    def test_two_demo_tasks_each_get_one_generator_call(self):
        """The supervisor reuses one Pipeline across tasks: the
        once-per-task guard must be keyed on the task, not the run."""
        calls: list = []
        pipeline = self.pipeline(lambda ctx: calls.append(ctx.task_id))
        ctx1 = StageContext("t1", self.work_dir / "t1", self.work_dir,
                            demo=True)
        ctx2 = StageContext("t2", self.work_dir / "t2", self.work_dir,
                            demo=True)
        self.assertTrue(pipeline._implement(ctx1, "7"))
        self.assertTrue(pipeline._implement(ctx1, "8"))  # same task: once
        self.assertTrue(pipeline._implement(ctx2, "7"))
        self.assertEqual(calls, ["t1", "t2"])

    def test_non_demo_task_is_untouched(self):
        calls: list = []
        pipeline = self.pipeline(lambda ctx: calls.append(ctx))
        ctx = StageContext("t1", self.work_dir / "task", self.work_dir)
        self.assertTrue(pipeline._implement(ctx, "7"))
        self.assertEqual(calls, [])
        self.assertEqual(
            pipeline.implement_prompts,
            [prompts.implement_slice(self.work_dir / "task", "7", 1, 5)])

    def test_raising_generator_does_not_stop_the_session(self):
        def broken(ctx):
            raise RuntimeError("generator exploded")

        pipeline = self.pipeline(broken)
        ctx = StageContext("t1", self.work_dir / "task", self.work_dir,
                           demo=True)
        self.assertTrue(pipeline._implement(ctx, "7"))
        self.assertEqual(len(pipeline.implement_prompts), 1)
        self.assertTrue(any("demo app generation failed" in m
                            and "generator exploded" in m
                            for m in self.messages))


class CompositionHookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.queue_dir = self.root / "queue"
        self.task_dir = self.queue_dir / "active" / "t1"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "original.md").write_text("a pizza site\n")
        write_legacy_linkage(task_dir_sidecar_path(self.task_dir),
                      SyncLinkage(issue=9, repo="acme/widgets", demo=True))
        self.workdir = self.root / "workdir"
        self.workdir.mkdir()

    def hook(self, api, content_generator, generator):
        params = DemoGenerationHookParams(
            queue_dir=self.queue_dir, apps_dir="demo-apps",
            repo="acme/widgets", content_model="cm",
            fallback_topic="Morris", app_model="am",
            output_dir=self.root / "out")
        return DemoAppGenerationHook(params, api,
                                     content_generator=content_generator,
                                     generator=generator,
                                     log=lambda m: None)

    def test_hook_builds_request_from_sidecar_and_ticket(self):
        seen: list = []

        class Api:
            def get_issue(self, number):
                return SimpleNamespace(title="Pizza Fan Site")

        def content_generator(params, request, **kw):
            seen.append(("content", params.content_model, request.title,
                         request.body))
            return _content()

        def generator(params, request, workdir, **kw):
            seen.append(("generate", params.repo, request.app_name,
                         str(workdir)))
            return "outcome"

        hook = self.hook(Api(), content_generator, generator)
        ctx = StageContext("t1", self.task_dir, self.workdir, demo=True)
        self.assertEqual(hook(ctx), "outcome")
        self.assertIn(("content", "cm", "Pizza Fan Site",
                       "a pizza site\n"), seen)
        self.assertIn(("generate", "acme/widgets", "pizza-fan-site",
                       str(self.workdir)), seen)

    def _app_name_seen(self, hook, ctx) -> list:
        seen: list = []

        def generator(params, request, workdir, **kw):
            seen.append(request.app_name)
            return "outcome"

        hook.generator = generator
        hook(ctx)
        return seen

    @staticmethod
    def _title_api():
        class Api:
            def get_issue(self, number):
                return SimpleNamespace(title="Pizza Fan Site")

        return Api()

    def test_hook_reuses_this_issues_placeholder_directory(self):
        """FR-2.4: the final app replaces the placeholder in place."""
        write_placeholder_app(self.workdir / "demo-apps",
                              "pizza-fan-site", "Pizza Fan Site", 9)
        hook = self.hook(self._title_api(), lambda *a, **k: _content(),
                         lambda *a, **k: "outcome")
        ctx = StageContext("t1", self.task_dir, self.workdir, demo=True)
        self.assertEqual(self._app_name_seen(hook, ctx), ["pizza-fan-site"])

    def test_hook_reuses_its_collision_placeholder_directory(self):
        """Edge case 8: the placeholder landed at <name>-<issue>."""
        write_placeholder_app(self.workdir / "demo-apps",
                              "pizza-fan-site-9", "Pizza Fan Site", 9)
        hook = self.hook(self._title_api(), lambda *a, **k: _content(),
                         lambda *a, **k: "outcome")
        ctx = StageContext("t1", self.task_dir, self.workdir, demo=True)
        self.assertEqual(self._app_name_seen(hook, ctx),
                         ["pizza-fan-site-9"])

    def test_hook_suffixes_when_the_bare_name_is_another_issue(self):
        """Edge case 8: the bare directory belongs to issue 77 — the
        final app must not clobber it."""
        write_placeholder_app(self.workdir / "demo-apps",
                              "pizza-fan-site", "Pizza Fan Site", 77)
        hook = self.hook(self._title_api(), lambda *a, **k: _content(),
                         lambda *a, **k: "outcome")
        ctx = StageContext("t1", self.task_dir, self.workdir, demo=True)
        self.assertEqual(self._app_name_seen(hook, ctx),
                         ["pizza-fan-site-9"])

    def test_unlinked_task_generates_nothing(self):
        hook = self.hook(object(), lambda *a, **k: None,
                         lambda *a, **k: "never")
        ctx = StageContext("ghost", self.task_dir, self.workdir, demo=True)
        self.assertIsNone(hook(ctx))


class FactoryGatingTest(unittest.TestCase):
    def _cfg(self, enabled: bool) -> Config:
        cfg = Config(
            work_dir=self.root, token_budget=100_000,
            max_spec_kickbacks=3, max_slice_implement=5,
            max_slice_tech_review=5, max_slice_func_review=5,
            max_slice_check_loops=3, autonomous_queue_target=5,
            trunk_branch="pi/trunk", task_provider="directory",
            directory_provider={},
            models={"technicalWriter": "m", "implementer": "m",
                    "assessor": "m"},
            model_context_map={}, repo_dir=self.root / "repo",
            raw={"demo": {"enabled": enabled}})
        return cfg

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "logs").mkdir()

    def test_disabled_feature_builds_no_generator(self):
        self.assertIsNone(build_demo_app_generator(self._cfg(False),
                                                   api=object()))

    def test_enabled_feature_builds_the_hook(self):
        hook = build_demo_app_generator(self._cfg(True), api=object(),
                                        log=lambda m: None)
        self.assertIsInstance(hook, DemoAppGenerationHook)


if __name__ == "__main__":
    unittest.main()
