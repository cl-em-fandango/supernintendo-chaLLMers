"""Slice 10 — in-flight handoff sync (spec FR-3 in-flight rule, NFR-1).

Each handoff event (Slice 6's comment posts) also triggers a targeted
per-task sync plus a full inbound pass through
`SyncEngine.on_stage_change(task_id)`, so an external halt is noticed
promptly while a session works. Covered here:

  * `HandoffSyncHook` unit behavior: post-then-pass for an in-flight task;
    a settled task posts but defers the pass to the move's stage-change
    hook (the summary is emitted exactly once per event, NFR-4);
  * the instrumented write sites driven directly — `continuation.write_note`,
    the `task_lifecycle` `Handoff` park, and the terminal executive-summary
    writers — with the real engine and a fake API: the prose is still
    written, the targeted + inbound pass ran, one summary line per event;
  * NFR-1: a raising fake API leaves the note, the move and the summary
    intact and raises nothing out of the hook;
  * FR-0.1/NFR-2: disabled config wires no hook and no engine, and the
    write sites are inert with zero sync calls.

All in-process: temp queue dirs and a fake API object (NFR-5).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import (  # noqa: E402
    Issue, IssueState, Label,
)
from harness.composition import (  # noqa: E402
    build, build_handoff_sync, build_sync_engine,
)
from harness.core.config import load  # noqa: E402
from harness.core.sync import SyncReport  # noqa: E402
from harness.core.sync_handoff_hook import HandoffSyncHook  # noqa: E402
from harness.core.sync_sidecar import (  # noqa: E402
    SyncLinkage,
    write_linkage,
)
from harness.core.sync_stage_change_hook import run_stage_change_hook  # noqa: E402
from harness.workflow.continuation import ContinuationNote, write_note  # noqa: E402
from harness.workflow.task_lifecycle import Handoff, TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels=(), state=IssueState.OPEN, body="issue body"):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The read/mutating surface the engine and poster use; calls recorded.

    `raising` turns every call into a transient failure (NFR-1 checks)."""

    def __init__(self, issues=(), labels=None, raising=False):
        self.issues = {issue.number: issue for issue in issues}
        self.labels = {number: list(names)
                       for number, names in (labels or {}).items()}
        self.comments = {}
        self.mutations = []
        self.reads = []
        self.raising = raising

    def _boom(self):
        if self.raising:
            raise RuntimeError("github is down")

    # -- reads -------------------------------------------------------------

    def list_issues(self, labels=(), state=IssueState.OPEN):
        self._boom()
        self.reads.append(("list_issues", tuple(labels), state))
        wanted = set(labels)
        return [issue for number, issue in sorted(self.issues.items())
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def get_issue(self, number):
        self._boom()
        self.reads.append(("get_issue", number))
        return self.issues[number]

    def list_labels(self, number):
        self._boom()
        self.reads.append(("list_labels", number))
        return [Label(name) for name in self.labels.get(number, [])]

    def list_comments(self, number):
        self._boom()
        self.reads.append(("list_comments", number))
        return list(self.comments.get(number, []))

    # -- mutations -----------------------------------------------------------

    def create_issue(self, title, body):
        self._boom()
        self.mutations.append(("create", title, body))
        number = max(self.issues, default=0) + 1
        self.issues[number] = _issue(number, title, body=body)
        self.labels.setdefault(number, [])
        return self.issues[number]

    def add_labels(self, number, labels):
        self._boom()
        self.mutations.append(("add", number, tuple(labels)))
        carried = self.labels.setdefault(number, [])
        self.labels[number] = carried + [n for n in labels if n not in carried]
        return [Label(n) for n in self.labels[number]]

    def remove_label(self, number, name):
        self._boom()
        self.mutations.append(("remove", number, name))
        self.labels[number] = [n for n in self.labels.get(number, [])
                               if n != name]

    def close_issue(self, number):
        self._boom()
        self.mutations.append(("close", number))
        issue = self.issues[number]
        self.issues[number] = _issue(number, issue.title,
                                     labels=tuple(l.name for l in issue.labels),
                                     state=IssueState.CLOSED, body=issue.body)
        return self.issues[number]

    def create_comment(self, number, body):
        self._boom()
        self.mutations.append(("comment", number, body))
        from external.github_api import Comment
        comment = Comment(id=1000 + len(self.mutations), body=body,
                          html_url=f"https://github.com/{REPO}/issues/{number}")
        self.comments.setdefault(number, []).append(comment)
        return comment

    # -- helpers for assertions ---------------------------------------------

    def comment_bodies(self, number):
        return [comment.body for comment in self.comments.get(number, [])]

    def inbound_reads(self):
        """Reads that are the full inbound pass (trigger-label listing)."""
        return [read for read in self.reads
                if read[0] == "list_issues" and "snes" in read[1]]


class RecordingPoster:
    """Records hook comment calls; optionally raises (NFR-1)."""

    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def __call__(self, task_id, stage, prose, slice_id=None, iteration=None):
        self.calls.append((task_id, stage, prose, slice_id, iteration))
        if self.raises:
            raise RuntimeError("poster exploded")


class RecordingEngine:
    """Records dispatcher calls; `in_flight` steers the hook's guard."""

    def __init__(self, in_flight=True, raises=False):
        self.in_flight = in_flight
        self.raises = raises
        self.calls = []

    def is_in_flight(self, task_id):
        return self.in_flight

    def on_stage_change(self, task_id=None):
        self.calls.append(task_id)
        if self.raises:
            raise RuntimeError("dispatcher down")
        return SyncReport(label_updates=1)


class HookUnitTest(unittest.TestCase):
    """`HandoffSyncHook` in isolation: order, guard, and NFR-1."""

    def setUp(self):
        self.messages = []

    def hook(self, engine=None, poster=None):
        return HandoffSyncHook(engine or RecordingEngine(),
                               poster or RecordingPoster(),
                               log=self.messages.append)

    def test_in_flight_handoff_posts_then_runs_targeted_pass(self):
        poster, engine = RecordingPoster(), RecordingEngine(in_flight=True)
        self.hook(engine, poster)("mover", "implement", "prose", "3", 2)
        self.assertEqual([("mover", "implement", "prose", "3", 2)],
                         poster.calls)
        self.assertEqual(["mover"], engine.calls)
        summaries = [m for m in self.messages
                     if m.startswith("github sync:")]
        self.assertEqual(1, len(summaries))

    def test_settled_task_posts_but_defers_the_pass_to_the_move_hook(self):
        """One event, one summary: a task that already left the in-flight
        locations is synced by its stage-change hook, never twice."""
        poster, engine = RecordingPoster(), RecordingEngine(in_flight=False)
        self.hook(engine, poster)("mover", "DONE", "summary prose")
        self.assertEqual([("mover", "DONE", "summary prose", None, None)],
                         poster.calls)
        self.assertEqual([], engine.calls)
        self.assertEqual([], [m for m in self.messages
                              if m.startswith("github sync:")])

    def test_raising_engine_is_logged_and_swallowed(self):
        poster = RecordingPoster()
        self.hook(RecordingEngine(raises=True), poster)("mover", "implement",
                                                        "prose")
        self.assertEqual(1, len(poster.calls))  # the comment still posted
        self.assertTrue(any("handoff sync failed" in m
                            for m in self.messages))
        self.assertEqual([], [m for m in self.messages
                              if m.startswith("github sync:")])

    def test_raising_poster_does_not_stop_the_pass(self):
        engine = RecordingEngine(in_flight=True)
        self.hook(engine, RecordingPoster(raises=True))("mover", "implement",
                                                        "prose")
        self.assertEqual(["mover"], engine.calls)
        self.assertTrue(any("handoff comment failed" in m
                            for m in self.messages))


class WriteSiteTest(unittest.TestCase):
    """The instrumented writers with the real engine, poster and hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "workDir": str(self.work_dir), "githubPat": "ghp_token",
            "githubRepo": REPO}))
        self.cfg = load(cfg_path)
        self.messages = []

    def active_task(self, name="mover", issue=7):
        task_dir = self.queue / "active" / name
        (task_dir / "artifacts" / "progress").mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text(f"# {name} body")
        write_linkage(task_dir / "gh.json", SyncLinkage(issue=issue,
                                                        repo=REPO))
        return task_dir

    def wire(self, api):
        """Engine + hook through the composition factories, fake API in."""
        engine = build_sync_engine(self.cfg, log=self.messages.append,
                                   api=api)
        hook = build_handoff_sync(engine, log=self.messages.append)
        return engine, hook

    def lifecycle(self, engine, hook):
        return TaskLifecycle(
            self.cfg, self.messages.append, handoff_sync=hook,
            stage_change_sync=lambda task_id: run_stage_change_hook(
                engine, task_id, log=self.messages.append))

    def summaries(self):
        return [m for m in self.messages if m.startswith("github sync:")]

    # -- continuation.write_note (in flight) ---------------------------------

    def test_write_note_runs_targeted_sync_and_full_inbound_pass(self):
        api = FakeApi(issues=[_issue(7, "Mover", labels=("snes",))],
                      labels={7: ["snes", "snes-pending"]})
        _engine, hook = self.wire(api)
        self.active_task()
        note = write_note(
            self.queue / "active" / "mover" / "artifacts" / "progress",
            ContinuationNote(stage="implement", attempt=1,
                             peak_tokens=90000, context_limit=80000,
                             task_id="mover"),
            "stopped mid-parser work", handoff_sync=hook)
        # The prose is still written, whatever the sync did.
        self.assertTrue(note.note_path.is_file())
        self.assertIn("stopped mid-parser work",
                      note.note_path.read_text())
        # The handoff comment posted in the FR-2.5 format.
        bodies = api.comment_bodies(7)
        self.assertEqual(1, len(bodies))
        self.assertTrue(bodies[0].startswith("**[implement]**"))
        self.assertIn("stopped mid-parser work", bodies[0])
        # Targeted sync: the active state label was applied.
        self.assertIn(("add", 7, ("snes-active",)), api.mutations)
        self.assertIn(("remove", 7, "snes-pending"), api.mutations)
        # Full inbound pass ran (trigger-label listing), so an external
        # halt would be noticed here.
        self.assertTrue(api.inbound_reads())
        # One summary line for this one event.
        self.assertEqual(1, len(self.summaries()))

    def test_retried_handover_posts_no_duplicate_comment(self):
        """AC-8 through the hook: identical prose dedups; the pass still
        runs and reports, but no second comment appears."""
        api = FakeApi(issues=[_issue(7, "Mover", labels=("snes",))],
                      labels={7: ["snes", "snes-active"]})
        _engine, hook = self.wire(api)
        self.active_task()
        notes_dir = self.queue / "active" / "mover" / "artifacts" / "progress"
        note = ContinuationNote(stage="implement", attempt=1,
                                peak_tokens=90000, task_id="mover")
        write_note(notes_dir, note, "same prose", handoff_sync=hook)
        write_note(notes_dir, note, "same prose", handoff_sync=hook)
        self.assertEqual(1, len(api.comment_bodies(7)))

    # -- terminal writers (park-with-handoff, exec summaries) ----------------

    def test_park_with_handoff_posts_once_and_logs_one_summary(self):
        api = FakeApi(issues=[_issue(7, "Mover", labels=("snes",))],
                      labels={7: ["snes", "snes-active"]})
        engine, hook = self.wire(api)
        self.active_task()
        self.lifecycle(engine, hook).park(
            "mover", "over the cap", handoff=Handoff(stage="implement"))
        self.assertTrue((self.queue / "parked" / "mover").is_dir())
        review = (self.queue / "review" / "mover.md").read_text()
        self.assertIn("## Handoff", review)  # prose still written
        bodies = api.comment_bodies(7)
        self.assertEqual(1, len(bodies))
        self.assertIn("## Handoff", bodies[0])
        # The move's stage-change pass relabelled the issue (parked state).
        self.assertIn(("add", 7, ("snes-parked",)), api.mutations)
        self.assertIn(("remove", 7, "snes-active"), api.mutations)
        # Hook + stage-change hook together: exactly one summary (NFR-4).
        self.assertEqual(1, len(self.summaries()))

    def test_complete_comments_summary_with_one_pass(self):
        api = FakeApi(issues=[_issue(7, "Mover", labels=("snes",))],
                      labels={7: ["snes", "snes-active"]})
        engine, hook = self.wire(api)
        self.active_task()
        self.lifecycle(engine, hook).complete("mover", "all slices green")
        self.assertTrue((self.queue / "done" / "mover").is_dir())
        bodies = api.comment_bodies(7)
        self.assertEqual(1, len(bodies))
        self.assertIn("all slices green", bodies[0])
        self.assertIn(("add", 7, ("snes-done",)), api.mutations)
        self.assertEqual(1, len(self.summaries()))

    def test_fail_comments_reason_with_one_pass(self):
        api = FakeApi(issues=[_issue(7, "Mover", labels=("snes",))],
                      labels={7: ["snes", "snes-active"]})
        engine, hook = self.wire(api)
        self.active_task()
        self.lifecycle(engine, hook).fail("mover", "verdict FAIL twice")
        self.assertTrue((self.queue / "failed" / "mover").is_dir())
        bodies = api.comment_bodies(7)
        self.assertEqual(1, len(bodies))
        self.assertIn("verdict FAIL twice", bodies[0])
        self.assertEqual(1, len(self.summaries()))

    # -- NFR-1: a raising API never breaks the handoff path -------------------

    def test_raising_api_still_writes_note_and_runs_the_path(self):
        api = FakeApi(raising=True)
        _engine, hook = self.wire(api)
        self.active_task()
        note = write_note(
            self.queue / "active" / "mover" / "artifacts" / "progress",
            ContinuationNote(stage="implement", attempt=1,
                             peak_tokens=90000, task_id="mover"),
            "prose survives", handoff_sync=hook)
        self.assertTrue(note.note_path.is_file())
        self.assertIn("prose survives", note.note_path.read_text())

    def test_raising_api_still_parks_with_its_summary(self):
        api = FakeApi(raising=True)
        engine, hook = self.wire(api)
        self.active_task()
        self.lifecycle(engine, hook).park("mover", "halted remotely")
        self.assertTrue((self.queue / "parked" / "mover").is_dir())
        self.assertIn("halted remotely",
                      (self.queue / "review" / "mover.md").read_text())

    # -- FR-0.1 / NFR-2: disabled config is inert -----------------------------

    def test_disabled_config_wires_no_engine_no_hook(self):
        cfg_path = self.work_dir / "disabled.json"
        cfg_path.write_text(json.dumps({"workDir": str(self.work_dir)}))
        cfg = load(cfg_path)
        self.assertFalse(cfg.github_sync_enabled)
        self.assertIsNone(build_sync_engine(cfg, log=self.messages.append))
        self.assertIsNone(build_handoff_sync(None))
        import os
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"HARNESS_CONFIG": str(cfg_path)}):
            _cfg, _store, _runner, _provider, pipeline, _log = build()
        self.assertIsNone(pipeline.handoff_sync)
        self.assertIsNone(pipeline.sync_engine)

    def test_enabled_build_wires_the_hook_sharing_the_engine(self):
        """The composition root hands the write sites the hook, and the
        hook's poster is the engine's own — one dedup map (FR-3)."""
        import os
        from unittest import mock
        cfg_path = self.work_dir / "config.json"
        with mock.patch.dict(os.environ,
                             {"HARNESS_CONFIG": str(cfg_path)}):
            _cfg, _store, _runner, _provider, pipeline, _log = build()
        hook = pipeline.handoff_sync
        self.assertIsInstance(hook, HandoffSyncHook)
        self.assertIs(hook.engine, pipeline.sync_engine)
        self.assertIs(hook.poster, pipeline.sync_engine.comment_poster)

    def test_write_sites_are_inert_without_a_hook(self):
        """Disabled wiring at every handoff site: prose written, zero
        sync calls (there is no engine, poster or API to call)."""
        self.active_task()
        note = write_note(
            self.queue / "active" / "mover" / "artifacts" / "progress",
            ContinuationNote(stage="implement", attempt=1,
                             peak_tokens=90000, task_id="mover"),
            "bare run prose")
        self.assertTrue(note.note_path.is_file())
        lifecycle = TaskLifecycle(self.cfg, self.messages.append)
        lifecycle.park("mover", "plain park")
        self.assertTrue((self.queue / "parked" / "mover").is_dir())
        self.assertEqual([], self.summaries())


if __name__ == "__main__":
    unittest.main()
