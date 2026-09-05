"""Slice 9 — end-to-end regression for the demo feature (AC 6, AC 7, AC 8).

One walk per configuration, all in-process (spec §6): temp git repos with
`git init --bare` fake origins, a fake `npm`, a fake `pi` on `PATH`, and an
injected GitHub transport. Nothing here talks to a model, to npm or to
GitHub.

  * demo enabled, `snes-demo` issue: inbound -> claim -> placeholder deploy
    -> (spec stages stubbed) -> app generation -> merge -> final deploy ->
    `done/`. Both success comments carry the derived
    `https://<owner>.github.io/<repo>/` URL, origin's deploy branch holds
    the built `docs/`, trunk holds the app source, and the deploy steps
    logged through the sink handed over by the composition root.
  * demo enabled, bare `snes` issue (AC 8): the walk completes and the
    repository is untouched by the feature — no `demo-apps/`, no `docs/`,
    no deploy branch, no comments, no label writes.
  * `demo.enabled = false` (FR-9): the `snes-demo` label is not a trigger
    at all, the composition root wires no demo hook, and a task that
    carries the demo flag anyway pushes nothing and comments nowhere.

The spec/slicing/slices stages are stubbed — they need real model output
and real slice artifacts, which is the waterfall's own coverage. Everything
the demo feature touches is the real code path: the composition-root
builders, the real hooks, the real deployer, the real merge and the real
holistic session (against the fake `pi`).
"""
from __future__ import annotations

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
from external.github_api import Issue, IssueState, Label
from harness import composition
from harness.core.config import Config
from harness.core.enums import Verdict
from harness.core.providers import DirectoryTaskProvider
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.core.sync_inbound import InboundParams, run_inbound
from harness.workflow.pipeline import Pipeline

REPO = "acme/widgets"
PAGES_URL = "https://acme.github.io/widgets/"
MODEL = "fake-model"
TITLE = "Pizza Fan Site"

FAKE_NPM = """#!/bin/sh
echo "npm $* cwd=$PWD" >> "$NPM_RECORD"
if [ "$1" = "install" ]; then
  echo '{"lockfileVersion": 3}' > package-lock.json
fi
if [ "$1" = "run" ] && [ "$2" = "build" ]; then
  mkdir -p build && echo '<html>built active app</html>' > build/index.html
fi
exit 0
"""

FAKE_PI = """#!/bin/sh
case " $* " in
  *" --list-models "*) echo "$PI_MODEL"; exit 0 ;;
esac
echo "$*" >> "$PI_RECORD"
printf '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"%s"}],"usage":{"inputTokens":10,"outputTokens":5,"totalTokens":15}}}\\n' "$PI_REPLY"
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


def _exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)


class FakeApi:
    """The injected GitHub transport: issue listing, titles, comments and
    label writes, every one of them recorded for the assertions."""

    def __init__(self, issues):
        self.issues = {issue.number: issue for issue in issues}
        self.comments: list[tuple[int, str]] = []
        self.added_labels: list[tuple[int, list[str]]] = []
        self.removed_labels: list[tuple[int, str]] = []

    def list_issues(self, labels=(), state=IssueState.OPEN):
        wanted = set(labels)
        return [issue for issue in sorted(self.issues.values(),
                                          key=lambda i: i.number)
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def list_labels(self, number):
        return [Label(name) for name in
                {label.name for label in self.issues[number].labels}]

    def add_labels(self, number, names):
        self.added_labels.append((number, list(names)))

    def remove_label(self, number, name):
        self.removed_labels.append((number, name))

    def get_issue(self, number):
        return self.issues[number]

    def create_comment(self, number, body):
        self.comments.append((number, body))


def _issue(number, title, labels):
    return Issue(number=number, title=title,
                 body=f"# {title}\n\nA small site about {title.lower()}.\n",
                 state=IssueState.OPEN,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


def _cfg(work_dir: Path, repo: Path, demo_enabled: bool) -> Config:
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
        models={"technicalWriter": MODEL, "implementer": MODEL,
                "assessor": MODEL},
        model_context_map={MODEL: 128_000},
        target_codebase_dir=repo,
        raw={"demo": {"enabled": demo_enabled,
                      "contentModel": MODEL,
                      "fallbackTopic": "History of Morris Dancing"}},
    )


class E2EPipeline(Pipeline):
    """The waterfall with the model-driven spec stages stubbed.

    `stage_slices` stands in for the implementer session by committing
    whatever the workdir holds (the generated app on the demo path), and
    runs the real `_generate_demo_app` call site first so the demo
    generation hook fires exactly where the real stage fires it.
    """

    def stage_spec(self, ctx):
        return True

    def stage_feasibility(self, ctx):
        return True

    def stage_slicing(self, ctx):
        return True

    def stage_slices(self, ctx):
        if ctx.demo:
            self._generate_demo_app(ctx)
        (Path(ctx.workdir) / "feature_note.md").write_text(
            f"# {ctx.task_id}\n")
        _git(ctx.workdir, "add", "-A")
        _git(ctx.workdir, "commit", "-m", "implement the slice")
        return True


class DemoE2EBase(unittest.TestCase):
    """Shared fixture: fake origin, work repo, fake npm, fake pi, queue."""

    demo_enabled = True
    labels: tuple[str, ...] = ("snes-demo",)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.messages: list[str] = []

        # The harness verification gate cannot judge a fixture repo; the
        # merge is what this walk exercises, the gate is covered elsewhere.
        original = git_cli.verify_harness
        git_cli.verify_harness = lambda workdir: (True, "fixture gate")
        self.addCleanup(setattr, git_cli, "verify_harness", original)

        self._install_path()
        self._build_repos()
        self._build_queue()

        self.api = FakeApi([_issue(7, TITLE, self.labels)])
        self.cfg = _cfg(self.root, self.repo, self.demo_enabled)
        self.provider = DirectoryTaskProvider(
            self.cfg.queue_dir / "pending",
            claimed_dir=self.cfg.queue_dir / "claimed",
            log=self.messages.append)

    # --- fixture ----------------------------------------------------

    def _install_path(self) -> None:
        self.bin = self.root / "bin"
        self.bin.mkdir()
        _exec(self.bin / "npm", FAKE_NPM)
        _exec(self.bin / "pi", FAKE_PI)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin}:{self._old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)
        for var, value in (("NPM_RECORD", str(self.root / "npm-calls.txt")),
                           ("PI_RECORD", str(self.root / "pi-calls.txt")),
                           ("PI_MODEL", MODEL),
                           ("PI_REPLY", "VERDICT: PASS")):
            os.environ[var] = value
            self.addCleanup(os.environ.pop, var, None)

    def _build_repos(self) -> None:
        self.origin = self.root / "origin.git"
        _git(self.root, "init", "--bare", "-b", "pi/trunk", str(self.origin))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("target repo\n")
        # gate_applies() only merges a repo shaped like the harness.
        (self.repo / "harness.py").write_text("# gate marker\n")
        (self.repo / "harness").mkdir()
        (self.repo / "harness" / "composition.py").write_text("# gate marker\n")
        _git(self.repo, "init", "-b", "pi/trunk")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init")
        _git(self.repo, "remote", "add", "origin", str(self.origin))

    def _build_queue(self) -> None:
        self.queue = self.root / "queue"
        for sub in ("pending", "claimed", "active", "review", "parked",
                    "failed", "done"):
            (self.queue / sub).mkdir(parents=True)

    # --- walk -------------------------------------------------------

    def inbound(self, demo_enabled: bool) -> int:
        return run_inbound(self.api, InboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append,
            demo_enabled=demo_enabled)).imported

    def claim(self):
        tasks = self.provider.fetch_pending(claim=True)
        return tasks[0] if tasks else None

    def pipeline(self) -> Pipeline:
        """The pipeline exactly as the composition root wires it."""
        api = self.api if self.cfg.demo.enabled else None
        return E2EPipeline(
            self.cfg,
            SessionRunner(self.cfg, StatsStore(self.cfg.stats_path),
                          log=self.messages.append),
            log=self.messages.append,
            provider=self.provider,
            placeholder_hook=composition.build_placeholder_hook(
                self.cfg, api=api, log=self.messages.append),
            demo_app_generator=composition.build_demo_app_generator(
                self.cfg, api=api, log=self.messages.append),
            final_deploy_hook=composition.build_final_deploy_hook(
                self.cfg, api=api, log=self.messages.append))

    def run_walk(self, demo_enabled: bool) -> tuple[str, object]:
        self.assertEqual(self.inbound(demo_enabled), 1,
                         "\n".join(self.messages))
        task = self.claim()
        self.assertIsNotNone(task, "\n".join(self.messages))
        return self.pipeline().process(task), task

    # --- assertions -------------------------------------------------

    def origin_docs(self) -> str:
        return _git(self.origin, "cat-file", "blob",
                    "pi/app-demo:docs/index.html")

    def origin_history_blobs(self) -> list[str]:
        """Every `docs/index.html` the deploy branch ever held."""
        blobs = []
        for rev in _git(self.origin, "rev-list", "pi/app-demo").split():
            probe = subprocess.run(
                ["git", "cat-file", "blob", f"{rev}:docs/index.html"],
                cwd=str(self.origin), capture_output=True, text=True)
            if probe.returncode == 0:
                blobs.append(probe.stdout)
        return blobs

    def comments(self) -> list[str]:
        return [body for _issue_number, body in self.api.comments]


class DemoSuccessWalkTest(DemoE2EBase):
    """AC 2 + AC 6 + AC 7: the whole feature, start to finish."""

    def test_full_walk_deploys_and_completes(self):
        outcome, task = self.run_walk(demo_enabled=True)

        # --- routing and queue shape --------------------------------
        self.assertEqual(outcome, "done", "\n".join(self.messages))
        self.assertTrue((self.queue / "done" / task.id).is_dir())
        self.assertFalse((self.queue / "failed" / task.id).exists())

        # --- the inbound chain (AC 1) --------------------------------
        self.assertTrue(task.meta.get("demo"),
                        "the snes-demo issue must claim as a demo task")

        # --- trunk holds the app source (FR-6.4) ---------------------
        trunk_files = _git(self.repo, "ls-tree", "-r", "--name-only",
                           "pi/trunk").split()
        self.assertTrue(any(f.startswith("demo-apps/") for f in trunk_files),
                        f"no app source on trunk: {trunk_files}")

        # --- origin holds the built artifacts in docs/ (FR-5, AC 6) --
        self.assertTrue(_ref_exists(self.origin, "pi/app-demo"))
        docs = self.origin_docs()
        self.assertIn("built active app", docs,
                      "docs/ is not the built app the fake npm produced")
        # The placeholder deployment landed first and the final deploy
        # replaced it in place (FR-2.4) — both are in the branch history.
        history = self.origin_history_blobs()
        self.assertGreaterEqual(len(history), 2,
                                "expected a placeholder and a final deploy")
        self.assertTrue(any(TITLE in blob for blob in history[1:]),
                        "the placeholder page never reached origin")
        self.assertNotIn(TITLE, history[0],
                         "the deployed head is still the placeholder")

        # --- both success comments, derived Pages URL (AC 7) ---------
        comments = self.comments()
        self.assertEqual(len(comments), 2, comments)
        self.assertTrue(any(c.startswith("Placeholder deployed — ")
                            and PAGES_URL in c for c in comments), comments)
        self.assertTrue(any(c.startswith("Deployed: ") and PAGES_URL in c
                            for c in comments), comments)

        # --- observability through the existing sink (FR-8.3) --------
        log = "\n".join(self.messages)
        self.assertIn("demo deploy: pushed pi/app-demo to origin", log)
        self.assertIn("demo deploy: artifacts ready", log)

        # --- no label writes beyond the harness's own ----------------
        self.assertEqual(self.api.added_labels, [])


class NonDemoInertTest(DemoE2EBase):
    """AC 8: with the feature switched on, a non-demo task is untouched."""

    labels = ("snes",)

    def test_non_demo_task_touches_nothing_demo(self):
        outcome, task = self.run_walk(demo_enabled=True)

        self.assertEqual(outcome, "done", "\n".join(self.messages))
        self.assertFalse(task.meta.get("demo"))
        # No app directory in the repo, on trunk or in the workdir.
        self.assertFalse((self.repo / "demo-apps").exists())
        trunk_files = _git(self.repo, "ls-tree", "-r", "--name-only",
                           "pi/trunk").split()
        self.assertFalse(any(f.startswith("demo-apps/")
                             for f in trunk_files), trunk_files)
        # No docs/, no deploy branch, nothing published.
        self.assertFalse(_ref_exists(self.origin, "pi/app-demo"))
        self.assertFalse((self.root / "demo-deploy").exists())
        # No comments, and no extra labels on the issue.
        self.assertEqual(self.comments(), [])
        self.assertEqual(self.api.added_labels, [])
        self.assertEqual(self.api.removed_labels, [])


class DemoDisabledTest(DemoE2EBase):
    """FR-9: `demo.enabled = false` — label ignored, hooks unwired."""

    demo_enabled = False

    def test_label_is_not_a_trigger(self):
        self.assertEqual(self.inbound(demo_enabled=False), 0)
        self.assertEqual(list((self.queue / "pending").glob("*.md")), [])
        self.assertIsNone(self.claim())

    def test_composition_wires_no_demo_hook(self):
        for builder in (composition.build_placeholder_hook,
                        composition.build_demo_app_generator,
                        composition.build_final_deploy_hook):
            self.assertIsNone(builder(self.cfg, api=self.api,
                                      log=self.messages.append), builder)

    def test_flagged_task_still_pushes_nothing(self):
        """A demo-flagged task with the feature off: no deploy, no comment."""
        # The flag can only be on the task itself (a sidecar written while
        # the feature was on); inbound cannot produce it now.
        pending = self.queue / "pending" / "pizza_fan_site.md"
        pending.write_text(f"# {TITLE}\n")
        outcome = self.pipeline().process(
            SimpleNamespace(id="pizza_fan_site", body=f"# {TITLE}",
                            source="directory:pizza_fan_site.md",
                            meta={"demo": True}))

        self.assertEqual(outcome, "done", "\n".join(self.messages))
        self.assertFalse(_ref_exists(self.origin, "pi/app-demo"))
        self.assertFalse((self.repo / "demo-apps").exists())
        self.assertEqual(self.comments(), [])


if __name__ == "__main__":
    unittest.main()
