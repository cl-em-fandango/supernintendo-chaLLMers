"""Slice 9 — lifecycle stage-change hooks.

Every queue-location transition triggers a sync pass automatically
(spec FR-3), and every hook site swallows sync errors so the task and
pipeline outcome are unchanged (NFR-1):

  * `TaskLifecycle.intake/park/fail/complete` (all pipeline park/fail/complete
    call sites route through these);
  * the resume/requeue move in `resume.py`;
  * the autonomous queue-fill in `autonomous.py` (a task only lands in
    `pending/`).

Everything runs in-process: temp dirs plus a fake API injected through the
composition root (`composition.build` + `composition.build_sync_engine`).
The lifecycle test drives intake -> active -> done through the *real*
`TaskLifecycle` and observes label updates with no manual `harness sync`.
Disabled config must produce zero hook activity and zero HTTP calls
(NFR-2 regression).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import (  # noqa: E402
    Comment, Issue, IssueState, Label,
)
from harness.composition import build, build_sync_engine  # noqa: E402
from harness.core.config import Config, load  # noqa: E402
from harness.core.enums import Verdict  # noqa: E402
from harness.core.providers import DirectoryTaskProvider, Task  # noqa: E402
from harness.core.session import SessionResult  # noqa: E402
from harness.core.sync import SyncEngine, SyncReport  # noqa: E402
from harness.core.sync_comments import HandoffCommentPoster  # noqa: E402
from harness.core.sync_stage_change_hook import run_stage_change_hook  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.workflow.autonomous import AutonomousGenerator  # noqa: E402
from harness.workflow.resume import resume_task  # noqa: E402
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels=(), state=IssueState.OPEN, body="issue body"):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The read/mutating surface the engine uses; calls recorded."""

    def __init__(self, issues=(), labels=None, comments=None):
        self.issues = {issue.number: issue for issue in issues}
        self.labels = {number: list(names)
                       for number, names in (labels or {}).items()}
        self.comments = dict(comments or {})
        self.mutations = []
        self.reads = []

    # -- reads -------------------------------------------------------------

    def list_issues(self, labels=(), state=IssueState.OPEN):
        self.reads.append(("list_issues", tuple(labels), state))
        wanted = set(labels)
        return [issue for number, issue in sorted(self.issues.items())
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def get_issue(self, number):
        self.reads.append(("get_issue", number))
        return self.issues[number]

    def list_labels(self, number):
        self.reads.append(("list_labels", number))
        return [Label(name) for name in self.labels.get(number, [])]

    def list_comments(self, number):
        self.reads.append(("list_comments", number))
        return list(self.comments.get(number, []))

    # -- mutations -----------------------------------------------------------

    def create_issue(self, title, body):
        self.mutations.append(("create", title, body))
        number = max(self.issues, default=0) + 1
        self.issues[number] = _issue(number, title, body=body)
        self.labels.setdefault(number, [])
        return self.issues[number]

    def add_labels(self, number, labels):
        self.mutations.append(("add", number, tuple(labels)))
        carried = self.labels.setdefault(number, [])
        self.labels[number] = carried + [n for n in labels if n not in carried]
        return [Label(n) for n in self.labels[number]]

    def remove_label(self, number, name):
        self.mutations.append(("remove", number, name))
        self.labels[number] = [n for n in self.labels.get(number, [])
                               if n != name]

    def close_issue(self, number):
        self.mutations.append(("close", number))
        issue = self.issues[number]
        self.issues[number] = _issue(number, issue.title,
                                     labels=tuple(l.name for l in issue.labels),
                                     state=IssueState.CLOSED, body=issue.body)
        return self.issues[number]

    def create_comment(self, number, body):
        self.mutations.append(("comment", number, body))
        comment = Comment(id=1000 + len(self.mutations), body=body,
                          html_url=f"https://github.com/{REPO}/issues/{number}")
        self.comments.setdefault(number, []).append(comment)
        return comment


class RecordingEngine:
    """Counts dispatcher calls and hands back an empty report."""

    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def on_stage_change(self, task_id=None):
        self.calls.append(task_id)
        if self.raises:
            raise RuntimeError("dispatcher down")
        return SyncReport()


class StubPipeline:
    """Records `process` calls; resume must still reach the pipeline."""

    def __init__(self):
        self.processed = []

    def process(self, task):
        self.processed.append(task.id)


class LifecycleHookTest(unittest.TestCase):
    """The real lifecycle, wired through the composition root, fake API."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "workDir": str(self.work_dir), "githubPat": "ghp_token",
            "githubRepo": REPO}))
        self.cfg_path = cfg_path

    def build_pipeline(self, api):
        """A real composition `Pipeline` whose engine talks to `api`."""
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(self.cfg_path)}):
            cfg, _store, _runner, _provider, pipeline, log = build()
        # The composition built a real HTTP client; swap in the fake at the
        # engine and the poster so no call can leave the process.
        pipeline.sync_engine = build_sync_engine(cfg, log=log, api=api)
        pipeline.handoff_sync = HandoffCommentPoster(api, cfg.queue_dir,
                                                     REPO, log=log)
        # Rewire the hook through the real wrapper, resolving the engine and
        # the lifecycle log at call time (the test swaps both afterwards).
        pipeline.lifecycle.stage_change_sync = (
            lambda task_id: run_stage_change_hook(
                pipeline.sync_engine, task_id,
                log=lambda m: pipeline.lifecycle.log(m)))
        pipeline.lifecycle.handoff_sync = pipeline.handoff_sync
        self.messages = []
        pipeline.lifecycle.log = self.messages.append
        return pipeline

    def test_intake_to_done_updates_labels_without_manual_sync(self):
        """AC-6 through the hook sites: driving intake -> active -> done
        through the *real* lifecycle relabels the issue, with no manual
        `harness sync` anywhere.

        Intake lands the task in `active/` (in-flight), so its hook runs the
        targeted pass; with no sidecar linkage yet that is a logged no-op
        (issue creation is the full pass's job, Slice 8). Completion lands
        the task in `done/` (settled), so its hook runs a full pass, which
        title-matches the issue, records the linkage and applies the label
        diff."""
        api = FakeApi(
            issues=[_issue(12, "Sync Hook Task", labels=("snes",))],
            labels={12: ["snes", "snes-pending", "human-label"]})
        pipeline = self.build_pipeline(api)
        task_dir = pipeline.lifecycle.intake(
            Task(id="sync_hook_task", body="body", source="test"))
        self.assertTrue(task_dir.is_dir())
        self.assertEqual([], api.mutations,
                         "an unlinked in-flight task must not be mutated")

        pipeline.lifecycle.complete("sync_hook_task", "all done")
        self.assertIn(("add", 12, ("snes-done",)), api.mutations)
        self.assertIn(("remove", 12, "snes-pending"), api.mutations)
        # Human labels are never touched (FR-2.4).
        self.assertIn("human-label", api.labels[12])
        self.assertTrue((self.queue / "done" / "sync_hook_task").is_dir())

    def test_each_hook_pass_logs_its_summary_exactly_once(self):
        api = FakeApi(
            issues=[_issue(12, "Sync Hook Task", labels=("snes",))],
            labels={12: ["snes", "snes-pending"]})
        pipeline = self.build_pipeline(api)
        pipeline.lifecycle.intake(
            Task(id="sync_hook_task", body="body", source="test"))
        pipeline.lifecycle.complete("sync_hook_task", "all done")
        summaries = [m for m in self.messages if m.startswith("github sync:")]
        self.assertEqual(2, len(summaries),
                         f"one summary per pass expected: {summaries}")

    def test_settled_hook_creates_the_issue_for_an_unlinked_task(self):
        """A task with no matching issue: the in-flight (intake) hook does
        not create one; the first hook that sees the task settled runs a
        full pass, which creates the issue and records the linkage."""
        api = FakeApi()
        pipeline = self.build_pipeline(api)
        pipeline.lifecycle.intake(
            Task(id="brand_new_task", body="body", source="test"))
        self.assertEqual([], api.mutations)
        pipeline.lifecycle.complete("brand_new_task", "all done")
        self.assertIn(("create", "brand new task", "body"), api.mutations)
        done_dir = self.queue / "done" / "brand_new_task"
        self.assertFalse((done_dir / "gh.json").exists(),
                         "the created issue was recorded in a sidecar")
        linkage = task_record.read_linkage(self.queue, "brand_new_task")
        self.assertIsNotNone(linkage)

    def _raising_lifecycle(self) -> tuple[TaskLifecycle, list]:
        """A lifecycle whose engine always raises; moves must be untouched.

        Wired exactly like the composition root: the lifecycle calls the
        hook wrapper, and the wrapper is what swallows (NFR-1)."""
        messages = []
        engine = RecordingEngine(raises=True)
        lifecycle = TaskLifecycle(
            self.cfg(), log=messages.append,
            stage_change_sync=lambda task_id: run_stage_change_hook(
                engine, task_id, log=messages.append))
        return lifecycle, messages

    def cfg(self):
        return load(self.cfg_path)

    def test_intake_survives_a_raising_hook(self):
        lifecycle, messages = self._raising_lifecycle()
        task_dir = lifecycle.intake(
            Task(id="raising_task", body="body", source="test"))
        self.assertTrue((task_dir / "task.json").is_file())
        self.assertTrue(any("github sync hook failed" in m for m in messages))

    def test_terminal_moves_survive_a_raising_hook(self):
        lifecycle, messages = self._raising_lifecycle()
        for move, where, text in (
                (lifecycle.park, "parked", "parked for the test"),
                (lifecycle.fail, "failed", "failed for the test"),
                (lifecycle.complete, "done", "completed for the test")):
            src = self.queue / "active" / "raising_task"
            if src.is_dir():
                import shutil
                shutil.rmtree(src)
            src.mkdir(parents=True)
            (src / "original.md").write_text("body")
            move("raising_task", text)
            self.assertTrue((self.queue / where / "raising_task").is_dir(),
                            f"{where} move changed by a failing hook")
        self.assertEqual(3, len([m for m in messages
                                 if "github sync hook failed" in m]))

    def test_park_hook_runs_after_the_move_and_the_handoff(self):
        """The park transition ends in `parked/`, comments the exec summary
        (Slice 6) and relabels the issue `snes-parked` (this slice)."""
        api = FakeApi(
            issues=[_issue(12, "Sync Hook Task", labels=("snes",))],
            labels={12: ["snes", "snes-active"]})
        pipeline = self.build_pipeline(api)
        task_dir = self.queue / "active" / "sync_hook_task"
        task_dir.mkdir(parents=True)
        (task_dir / "original.md").write_text("body")
        pipeline.lifecycle.park("sync_hook_task", "parked via GitHub issue #12")
        self.assertTrue((self.queue / "parked" / "sync_hook_task").is_dir())
        self.assertIn(("add", 12, ("snes-parked",)), api.mutations)
        self.assertIn(("remove", 12, "snes-active"), api.mutations)


class ResumeHookTest(unittest.TestCase):
    """The resume/requeue move fires the hook once (spec FR-3)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({"workDir": str(self.work_dir)}))
        self.cfg = load(cfg_path)
        self.lifecycle = TaskLifecycle(self.cfg, log=lambda _m: None)
        self.messages = []

    def parked_task(self, task_id: str) -> None:
        task_dir = self.queue / "parked" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "original.md").write_text("body")
        (task_dir / "task.json").write_text(json.dumps(
            {"id": task_id, "status": "parked", "source": "test"}))

    def test_resume_fires_the_hook_after_the_move(self):
        self.parked_task("resumed_task")
        engine = RecordingEngine()
        pipeline = StubPipeline()
        code = resume_task("resumed_task", True, self.cfg, pipeline,
                           lifecycle=self.lifecycle, log=self.messages.append,
                           sync_engine=engine)
        self.assertEqual(0, code)
        self.assertEqual(["resumed_task"], engine.calls)
        self.assertTrue((self.queue / "active" / "resumed_task").is_dir())
        self.assertEqual(["resumed_task"], pipeline.processed)

    def test_resume_survives_a_raising_hook(self):
        self.parked_task("resumed_task")
        pipeline = StubPipeline()
        code = resume_task("resumed_task", True, self.cfg, pipeline,
                           lifecycle=self.lifecycle, log=self.messages.append,
                           sync_engine=RecordingEngine(raises=True))
        self.assertEqual(0, code)
        self.assertTrue((self.queue / "active" / "resumed_task").is_dir())
        self.assertEqual(["resumed_task"], pipeline.processed)

    def test_resume_without_an_engine_is_a_no_op(self):
        self.parked_task("resumed_task")
        code = resume_task("resumed_task", True, self.cfg, StubPipeline(),
                           lifecycle=self.lifecycle,
                           log=self.messages.append)
        self.assertEqual(0, code)
        self.assertTrue((self.queue / "active" / "resumed_task").is_dir())


class AutonomousHookTest(unittest.TestCase):
    """A task landing in `pending/` from the generator fires the hook."""

    class PassingRunner:
        """Suggest returns a proposal; review passes it."""

        def run(self, model, workdir, prompt, *, task_id=None, stage=None,
                notes="", **kwargs):
            verdict = (Verdict.DONE if "SUGGEST" in str(stage).upper()
                       else Verdict.PASS)
            output = ("# Ship the widget\n\nDetails.\n\n## Summary\n\n"
                      f"VERDICT: {verdict.value}")
            return SessionResult(ok=True, verdict=verdict, peak_tokens=0,
                                 duration_s=0.0, output=output,
                                 out_file=Path(workdir) / "session.out")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        self.pending = self.queue / "pending"
        self.claimed = self.queue / "claimed"
        self.pending.mkdir(parents=True)
        self.claimed.mkdir(parents=True)
        self.cfg = Config(
            work_dir=self.work_dir,
            token_budget=100_000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=1,
            trunk_branch="pi/trunk",
            task_provider="directory",
            directory_provider={},
            models={"technicalWriter": "m", "implementer": "m", "assessor": "m",
                    "randomPool": ["model-a", "model-b"]},
            model_context_map={},
        )
        self.messages = []

    def generator(self, engine):
        provider = DirectoryTaskProvider(self.pending, self.claimed,
                                         log=self.messages.append)
        return AutonomousGenerator(self.cfg, self.PassingRunner(), provider,
                                   log=self.messages.append,
                                   sync_engine=engine)

    def test_queued_task_fires_the_hook(self):
        engine = RecordingEngine()
        self.assertEqual(1, self.generator(engine).run(self.work_dir))
        self.assertEqual(1, len(engine.calls))
        self.assertTrue(engine.calls[0].startswith("auto-1-"))
        self.assertTrue(list(self.pending.glob("auto-1-*.md")))

    def test_autonomous_survives_a_raising_hook(self):
        engine = RecordingEngine(raises=True)
        self.assertEqual(1, self.generator(engine).run(self.work_dir))
        self.assertEqual(1, len(list(self.pending.glob("auto-1-*.md"))),
                         "a failing hook changed what the run queued")


class DisabledConfigRegressionTest(unittest.TestCase):
    """GitHub unconfigured: hooks are inert and no HTTP is reachable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({"workDir": str(self.work_dir)}))
        self.cfg_path = cfg_path

    def test_full_lifecycle_with_github_disabled_makes_no_http_calls(self):
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(self.cfg_path)}):
            with mock.patch("harness.composition.build_github_api",
                            side_effect=AssertionError("HTTP built")):
                cfg, _store, _runner, _provider, pipeline, log = build()
        self.assertIsNone(pipeline.sync_engine)
        self.assertIsNone(pipeline.lifecycle.stage_change_sync)
        self.assertIsNone(pipeline.lifecycle.handoff_sync)
        messages = []
        pipeline.lifecycle.log = messages.append
        # `complete`/`park` each move from `active/`, so two tasks cover
        # both terminal transitions with the sync wiring absent.
        task_dir = pipeline.lifecycle.intake(
            Task(id="quiet_task", body="body", source="test"))
        pipeline.lifecycle.complete("quiet_task", "done while disabled")
        other_dir = pipeline.lifecycle.intake(
            Task(id="quiet_park", body="body", source="test"))
        pipeline.lifecycle.park("quiet_park", "parked while disabled")
        self.assertTrue((self.work_dir / "queue" / "done"
                         / "quiet_task").is_dir())
        self.assertTrue((self.work_dir / "queue" / "parked"
                         / "quiet_park").is_dir())
        self.assertFalse(task_dir.exists())
        self.assertFalse(other_dir.exists())
        self.assertFalse([m for m in messages
                          if "github sync" in m or "hook failed" in m])

    def test_resume_and_autonomous_disabled_paths_stay_inert(self):
        """The non-lifecycle hook sites with no engine: no raise, no sync."""
        engine = None
        # resume: engine None is the disabled wiring (already covered by
        # ResumeHookTest.test_resume_without_an_engine_is_a_no_op); assert
        # the hook helper itself short-circuits on a None engine.
        from harness.core.sync_stage_change_hook import run_stage_change_hook
        messages = []
        run_stage_change_hook(engine, "any_task", log=messages.append)
        self.assertEqual([], messages)


if __name__ == "__main__":
    unittest.main()
