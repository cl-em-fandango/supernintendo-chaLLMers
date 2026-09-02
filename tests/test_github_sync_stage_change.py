"""Slice 8 — `on_stage_change()` orchestration + targeted per-task sync.

The engine is the dispatcher the hook sites (Slices 9–10) call; it picks
the pass the trigger needs (spec FR-3):

  * no task id (manual / stage change) -> one full two-way pass;
  * an in-flight task id (`claimed/`, `active/`) -> a targeted sync for
    that task (its state label + handoff comment) followed by a full
    inbound pass, so external halts are noticed promptly;
  * any other task id -> a full pass.

Everything runs in-process: temp dirs plus a fake API injected through
the composition root (`composition.build_sync_engine`), and the engine
is reached from `composition.build()` without a module-level singleton.
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
    Comment, GitHubRateLimitError, Issue, IssueState, Label,
)
from harness.composition import build, build_sync_engine  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.sync import SyncEngine  # noqa: E402
from harness.core.sync_comments import HandoffCommentPoster  # noqa: E402
from harness.core import task_record  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    SyncLinkage, file_sidecar_path, task_dir_sidecar_path,
    write_legacy_linkage,
)

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels=(), state=IssueState.OPEN, body="issue body"):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The read/mutating surface both dispatch modes use; calls recorded."""

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


class SyncEngineTestCase(unittest.TestCase):
    """`on_stage_change()` against temp dirs + fake API."""

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

    def engine(self, api, poster=None):
        return SyncEngine(self.cfg, api, log=self.messages.append,
                          comment_poster=poster)

    def file_task(self, name, location="pending", issue=None):
        path = self.queue / location / f"{name}.md"
        path.write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    def dir_task(self, name, location="active", issue=None):
        path = self.queue / location / name
        path.mkdir(parents=True)
        (path / "original.md").write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(task_dir_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    # -- no task id: full pass -------------------------------------------

    def test_no_task_id_runs_a_full_inbound_and_outbound_pass(self):
        # #7 imports (inbound); `legacy_task` has no issue at all, so the
        # outbound phase creates and labels it.
        api = FakeApi(issues=[_issue(7, "Sync feature",
                                     labels=("snes", "snes-pending"))],
                      labels={7: ["snes", "snes-pending"]})
        self.file_task("legacy_task")
        report = self.engine(api).on_stage_change()
        self.assertEqual(1, report.imported)
        self.assertEqual(1, report.created_issues)
        self.assertEqual(1, report.label_updates)
        self.assertFalse(report.aborted)
        self.assertTrue((self.queue / "pending" / "Sync_feature.md").is_file())
        self.assertIn(("add", 8, ("snes-pending",)), api.mutations)

    def test_full_pass_over_an_empty_queue_makes_no_calls(self):
        api = FakeApi()
        report = self.engine(api).on_stage_change()
        self.assertEqual(0, report.imported)
        self.assertEqual([], api.mutations)

    # -- in-flight task id: targeted + inbound ----------------------------

    def test_in_flight_task_runs_targeted_sync_then_inbound(self):
        """The in-flight rule (FR-3): only this task's state label moves —
        no outbound listing/scan of the other tasks — and the full inbound
        pass still runs so an external halt is noticed."""
        api = FakeApi(
            issues=[_issue(3, "In flight task", labels=("snes",)),
                    _issue(9, "Other open issue", labels=("snes-human",))],
            labels={3: ["snes", "snes-claimed"]})
        self.dir_task("in_flight_task", location="active", issue=3)
        report = self.engine(api).on_stage_change("in_flight_task")
        # Targeted: the linked issue was read by number and relabelled.
        self.assertIn(("get_issue", 3), api.reads)
        self.assertIn(("add", 3, ("snes-active",)), api.mutations)
        self.assertEqual(1, report.label_updates)
        # Targeted, not full outbound: the open/closed queue-wide listings
        # and the other task's issue were never touched.
        self.assertNotIn(("list_issues", (), IssueState.OPEN), api.reads)
        self.assertNotIn(("list_issues", (), IssueState.CLOSED), api.reads)
        self.assertNotIn(("get_issue", 9), api.reads)
        # Followed by the full inbound pass: the ingest listing ran.
        self.assertIn(("list_issues", ("snes",), IssueState.OPEN), api.reads)
        self.assertFalse(report.aborted)

    def test_claimed_location_counts_as_in_flight(self):
        api = FakeApi(issues=[_issue(4, "Claimed task", labels=("snes",))],
                      labels={4: []})
        self.file_task("claimed_task", location="claimed", issue=4)
        report = self.engine(api).on_stage_change("claimed_task")
        self.assertIn(("add", 4, ("snes-claimed",)), api.mutations)
        self.assertEqual(1, report.label_updates)
        self.assertIn(("list_issues", ("snes",), IssueState.OPEN), api.reads)

    def test_targeted_sync_posts_the_pending_handoff_comment(self):
        """The comment half of the targeted sync: an event whose post
        failed is retried for this task and deduped via the sidecar."""
        api = FakeApi(issues=[_issue(5, "In flight task", labels=("snes",))],
                      labels={5: ["snes", "snes-active"]})
        task = self.dir_task("in_flight_task", location="active", issue=5)
        poster = HandoffCommentPoster(api, self.queue, REPO,
                                      log=self.messages.append)
        with mock.patch.object(api, "create_comment",
                               side_effect=RuntimeError("transport down")):
            poster("in_flight_task", "active", "handoff prose")
        self.assertEqual([], api.mutations)

        report = self.engine(api, poster=poster).on_stage_change(
            "in_flight_task")
        self.assertEqual(1, report.comments_posted)
        self.assertTrue(any(m[0] == "comment" and "handoff prose" in m[2]
                            for m in api.mutations))
        self.assertFalse((task / "gh.json").exists(),
                         "the poster wrote a task-dir sidecar")
        linkage = task_record.read_linkage(self.queue, "in_flight_task")
        self.assertEqual(1, len(linkage.comment_ids))
        # A second pass has nothing pending and the dedup map holds the id:
        # no duplicate comment (FR-2.5).
        again = self.engine(api, poster=poster).on_stage_change(
            "in_flight_task")
        self.assertEqual(0, again.comments_posted)
        self.assertEqual(1, len([m for m in api.mutations
                                 if m[0] == "comment"]))

    def test_in_flight_task_without_linkage_is_a_logged_no_op(self):
        """No issue to label yet: targeted sync skips, inbound still runs."""
        api = FakeApi()
        self.dir_task("orphan_task", location="active")
        report = self.engine(api).on_stage_change("orphan_task")
        self.assertEqual(0, report.label_updates)
        self.assertEqual([], api.mutations)
        self.assertIn(("list_issues", ("snes",), IssueState.OPEN), api.reads)
        self.assertTrue(any("no issue" in m for m in self.messages))

    def test_closed_linked_issue_is_left_to_the_full_pass(self):
        """Parking an in-flight task mid-session is the full pass's call;
        the targeted sync only logs and moves on to the inbound phase."""
        api = FakeApi(issues=[_issue(6, "In flight task",
                                     labels=("snes",), state=IssueState.CLOSED)],
                      labels={6: ["snes-active"]})
        self.dir_task("in_flight_task", location="active", issue=6)
        report = self.engine(api).on_stage_change("in_flight_task")
        self.assertEqual(0, report.label_updates)
        self.assertEqual(0, report.parked)
        self.assertEqual([], api.mutations)
        self.assertTrue((self.queue / "active" / "in_flight_task").is_dir())

    def test_rate_limit_in_the_targeted_phase_aborts_the_pass(self):
        """Edge 9 through the dispatcher: reported, not raised; the
        inbound phase rolls to the next pass."""
        api = FakeApi(issues=[_issue(3, "In flight task", labels=("snes",))])
        self.dir_task("in_flight_task", location="active", issue=3)
        api.get_issue = mock.Mock(
            side_effect=GitHubRateLimitError("github rate limit exceeded"))
        report = self.engine(api).on_stage_change("in_flight_task")
        self.assertTrue(report.aborted)
        self.assertIn("ABORTED", report.summary_line())
        self.assertNotIn(("list_issues", ("snes",), IssueState.OPEN),
                         api.reads)

    # -- settled task id: full pass ---------------------------------------

    def test_settled_task_id_runs_a_full_pass(self):
        """A task in `pending/` is not in flight: the trigger gets the
        full two-way pass, including the queue-wide outbound listings."""
        api = FakeApi(issues=[_issue(7, "Sync feature",
                                     labels=("snes", "snes-pending"))],
                      labels={7: ["snes", "snes-pending"]})
        self.file_task("settled_task", location="pending", issue=7)
        report = self.engine(api).on_stage_change("settled_task")
        self.assertIn(("list_issues", (), IssueState.OPEN), api.reads)
        self.assertIn(("list_issues", (), IssueState.CLOSED), api.reads)
        self.assertEqual(0, report.imported)

    def test_unknown_task_id_falls_back_to_a_full_pass(self):
        api = FakeApi()
        self.engine(api).on_stage_change("never_seen")
        self.assertIn(("list_issues", ("snes",), IssueState.OPEN), api.reads)


class CompositionWiringTest(unittest.TestCase):
    """The engine is reachable from the composition root, never a global."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

    def _config(self, raw: dict) -> Path:
        cfg_path = self.work / "config.json"
        cfg_path.write_text(json.dumps({"workDir": str(self.work), **raw}))
        return cfg_path

    def test_build_exposes_one_shared_engine(self):
        cfg_path = self._config({"githubPat": "ghp_token",
                                 "githubRepo": REPO})
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            cfg, _store, _runner, _provider, pipeline, _log = build()
        self.assertTrue(cfg.github_sync_enabled)
        self.assertIsInstance(pipeline.sync_engine, SyncEngine)
        # One poster instance shared by the write sites and the dispatcher;
        # the write sites hold the handoff hook, which posts through that
        # poster and dispatches through the one shared engine (Slice 10).
        self.assertIs(pipeline.sync_engine.comment_poster,
                      pipeline.handoff_sync.poster)
        self.assertIs(pipeline.sync_engine, pipeline.handoff_sync.engine)
        # No module-level singleton: a second build is a second instance.
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            _cfg, _s, _r, _p, other, _l = build()
        self.assertIsNot(other.sync_engine, pipeline.sync_engine)

    def test_factory_returns_the_injected_api_engine(self):
        cfg_path = self._config({"githubPat": "ghp_token",
                                 "githubRepo": REPO})
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            cfg, *_rest = build()
        api = FakeApi()
        engine = build_sync_engine(cfg, log=self._noop, api=api)
        self.assertIsInstance(engine, SyncEngine)
        self.assertIs(api, engine.api)
        engine.on_stage_change()
        self.assertEqual([], api.mutations)

    def test_disabled_config_builds_no_engine_and_no_api(self):
        cfg_path = self._config({})
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            cfg, *_rest = build()
            with mock.patch("harness.composition.build_github_api",
                            side_effect=AssertionError("HTTP built")):
                engine = build_sync_engine(cfg, log=self._noop)
        self.assertIsNone(engine)

    def test_disabled_pipeline_carries_no_engine(self):
        cfg_path = self._config({})
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            _cfg, _store, _runner, _provider, pipeline, _log = build()
        self.assertIsNone(pipeline.sync_engine)
        self.assertIsNone(pipeline.handoff_sync)

    @staticmethod
    def _noop(_message: str) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
