"""Slice 5 — outbound: every queue task gets one issue with one state label.

Covers spec FR-2.1 (normalized title matching, lowest number wins with a
warning), FR-2.2 (a closed match parks instead of recreating; AC-7),
FR-2.3 (create with the task body and a recorded sidecar; AC-5),
FR-2.4 (diff-based state labels: stale `snes-*` state labels removed,
human labels and the bare `snes` marker never touched; AC-6, edge 10
rename repair, `snes-parked` state idempotency), FR-2.6 (no orphan
chasing) and NFR-1 (a failing task is logged, the pass continues).
All tests run in-process: temp queue directories and a fake API object
recording every mutating call (NFR-5).

Done-when checks covered here:
  * AC-5: `pending/fix_the_parser.md` with no issue -> open issue titled
    `fix the parser`, task body as body, label `snes-pending`;
  * AC-6: active -> done moves the label to exactly `snes-active` then
    `snes-done`, stale label removed, human labels untouched;
  * AC-7: only a closed issue matches -> task parked, nothing created;
  * a pass over an empty queue makes zero API calls at all.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Issue, IssueState, Label  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.interrupt import read_interrupt  # noqa: E402
from harness.core.sync import sync_pass  # noqa: E402
from harness.core.sync_outbound import (  # noqa: E402
    OutboundParams,
    run_outbound,
)
from harness.core.sync_sidecar import (  # noqa: E402
    SyncLinkage,
    file_sidecar_path,
    read_linkage,
    write_linkage,
)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, state=IssueState.OPEN, body="issue body"):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=(), html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The read/mutating surface outbound uses; mutations are recorded."""

    def __init__(self, issues=(), labels=None):
        self.issues = {issue.number: issue for issue in issues}
        self.labels = {number: list(names)
                       for number, names in (labels or {}).items()}
        self.mutations = []

    # -- reads -------------------------------------------------------------

    def list_issues(self, labels=(), state=IssueState.OPEN):
        wanted = set(labels)
        return [issue for number, issue in sorted(self.issues.items())
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def get_issue(self, number):
        return self.issues[number]

    def list_labels(self, number):
        return [Label(name) for name in self.labels.get(number, [])]

    # -- mutations (recorded) ------------------------------------------------

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


class OutboundTestCase(unittest.TestCase):
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

    def params(self):
        return OutboundParams(queue_dir=self.queue, repo=REPO,
                              log=self.messages.append,
                              lifecycle=TaskLifecycle(self.cfg,
                                                      log=self.messages.append))

    def file_task(self, location, name, issue=None, body=None):
        path = self.queue / location / f"{name}.md"
        path.write_text(body if body is not None else f"# {name} body")
        if issue is not None:
            write_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    def dir_task(self, location, name, issue=None):
        task_dir = self.queue / location / name
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text(f"# {name} body")
        if issue is not None:
            write_linkage(task_dir / "gh.json",
                          SyncLinkage(issue=issue, repo=REPO))
        return task_dir

    def carried(self, api, number):
        return set(api.labels.get(number, []))


class CreateTest(OutboundTestCase):
    def test_ac5_task_without_issue_creates_issue_and_records_sidecar(self):
        task = self.file_task("pending", "fix_the_parser",
                              body="# parser fix\n\nsteps\n")
        api = FakeApi()
        result = run_outbound(api, self.params())
        self.assertEqual(1, result.created_issues)
        issue = api.get_issue(1)
        self.assertEqual("fix the parser", issue.title)
        self.assertEqual("# parser fix\n\nsteps\n", issue.body)
        self.assertEqual({"snes-pending"}, self.carried(api, 1))
        self.assertEqual(1, read_linkage(file_sidecar_path(task)).issue)
        # Idempotent: a second pass mutates nothing.
        api.mutations.clear()
        self.assertEqual(
            0, run_outbound(api, self.params()).created_issues
            + run_outbound(api, self.params()).label_updates)
        self.assertEqual([], api.mutations)

    def test_dir_task_creates_from_original_md_and_records_gh_json(self):
        task_dir = self.dir_task("active", "live_task")
        api = FakeApi()
        self.assertEqual(1, run_outbound(api, self.params()).created_issues)
        self.assertEqual("# live_task body", api.get_issue(1).body)
        self.assertEqual(1, read_linkage(task_dir / "gh.json").issue)
        self.assertEqual({"snes-active"}, self.carried(api, 1))

    def test_sidecar_pointing_at_another_repo_is_skipped(self):
        self.file_task("pending", "foreign", issue=50)
        write_linkage(self.queue / "pending" / "foreign.md.gh.json",
                      SyncLinkage(issue=50, repo="other/repo"))
        api = FakeApi()
        result = run_outbound(api, self.params())
        self.assertEqual(0, result.created_issues + result.label_updates)
        self.assertEqual([], api.mutations)


class StateLabelTest(OutboundTestCase):
    def test_ac6_label_moves_active_then_done_human_labels_untouched(self):
        task_dir = self.dir_task("active", "mover", issue=1)
        api = FakeApi([_issue(1, "mover")],
                      labels={1: ["snes", "snes-pending", "bug"]})
        result = run_outbound(api, self.params())
        self.assertEqual(1, result.label_updates)
        self.assertEqual({"snes", "snes-active", "bug"}, self.carried(api, 1))
        shutil.move(str(task_dir), str(self.queue / "done" / "mover"))
        result = run_outbound(api, self.params())
        self.assertEqual(1, result.label_updates)
        self.assertEqual({"snes", "snes-done", "bug"}, self.carried(api, 1))

    def test_resume_from_parked_removes_parked_label_keeps_bare_snes(self):
        self.dir_task("active", "resumed", issue=6)
        api = FakeApi([_issue(6, "resumed")],
                      labels={6: ["snes", "snes-parked"]})
        run_outbound(api, self.params())
        self.assertEqual({"snes", "snes-active"}, self.carried(api, 6))
        self.assertIn(("remove", 6, "snes-parked"), api.mutations)

    def test_parked_state_label_is_idempotent(self):
        self.dir_task("parked", "sleeping", issue=4)
        api = FakeApi([_issue(4, "sleeping")], labels={4: ["snes-parked"]})
        result = run_outbound(api, self.params())
        self.assertEqual(0, result.label_updates)
        self.assertEqual([], api.mutations)
        self.assertEqual({"snes-parked"}, self.carried(api, 4))

    def test_edge10_renamed_away_state_label_is_reapplied(self):
        self.dir_task("active", "relabeled", issue=7)
        api = FakeApi([_issue(7, "relabeled")], labels={7: ["bug"]})
        self.assertEqual(1, run_outbound(api, self.params()).label_updates)
        self.assertEqual({"bug", "snes-active"}, self.carried(api, 7))

    def test_title_match_records_sidecar_and_second_pass_is_quiet(self):
        task = self.file_task("pending", "legacy_task")
        api = FakeApi([_issue(9, "Legacy Task")])
        self.assertEqual(1, run_outbound(api, self.params()).label_updates)
        self.assertEqual(9, read_linkage(file_sidecar_path(task)).issue)
        api.mutations.clear()
        run_outbound(api, self.params())
        self.assertEqual([], api.mutations)


class ClosedMatchTest(OutboundTestCase):
    def test_ac7_closed_match_parks_and_nothing_is_created(self):
        task = self.file_task("pending", "old_task", body="# old body")
        api = FakeApi([_issue(2, "old task", state=IssueState.CLOSED)])
        result = run_outbound(api, self.params())
        self.assertEqual(1, result.parked)
        self.assertEqual(0, result.created_issues)
        self.assertFalse(task.exists())
        parked = self.queue / "parked" / "old_task"
        self.assertEqual("# old body", (parked / "original.md").read_text())
        self.assertEqual("parked",
                         json.loads((parked / "task.json").read_text())["status"])
        self.assertIn("GitHub issue closed",
                      (self.queue / "review" / "old_task.md").read_text())
        self.assertEqual(2, read_linkage(parked / "gh.json").issue)
        # Idempotent: already parked + closed -> silent no-op, no create.
        api.mutations.clear()
        self.assertEqual(0, run_outbound(api, self.params()).parked)
        self.assertEqual([], api.mutations)

    def test_closed_match_parks_active_task_without_standing_down(self):
        task_dir = self.dir_task("active", "midflight", issue=3)
        api = FakeApi([_issue(3, "midflight", state=IssueState.CLOSED)])
        self.assertEqual(1, run_outbound(api, self.params()).parked)
        self.assertFalse(task_dir.exists())
        self.assertTrue((self.queue / "parked" / "midflight").is_dir())
        # Edge 2: closes are not a halt — no stand-down flag was written.
        self.assertIsNone(read_interrupt(self.work_dir))


class MatchingTest(OutboundTestCase):
    def test_multiple_open_matches_lowest_number_with_warning(self):
        task = self.file_task("pending", "twin")
        api = FakeApi([_issue(5, "twin"), _issue(2, "TWIN"),
                       _issue(8, "unrelated")])
        run_outbound(api, self.params())
        self.assertEqual({"snes-pending"}, self.carried(api, 2))
        self.assertEqual(set(), self.carried(api, 5))
        self.assertEqual(2, read_linkage(file_sidecar_path(task)).issue)
        self.assertTrue(any("match" in m and "#2" in m
                            for m in self.messages))

    def test_sidecar_wins_even_when_the_issue_left_both_listings(self):
        self.file_task("pending", "tracked", issue=12)
        api = FakeApi()  # listings empty; get_issue still knows #12
        api.issues[12] = _issue(12, "Totally Renamed")
        run_outbound(api, self.params())
        self.assertEqual({"snes-pending"}, self.carried(api, 12))
        self.assertEqual(0,
                         sum(1 for m in api.mutations if m[0] == "create"))

    def test_review_summary_does_not_stomp_the_done_task_label(self):
        self.dir_task("done", "finished", issue=8)
        (self.queue / "review" / "finished.md").write_text("# summary")
        api = FakeApi([_issue(8, "finished")], labels={8: ["snes-done"]})
        result = run_outbound(api, self.params())
        self.assertEqual(0, result.label_updates)
        self.assertEqual([], api.mutations)
        self.assertFalse((self.queue / "review" / "finished.md.gh.json")
                         .exists())


class PassHealthTest(OutboundTestCase):
    def test_empty_queue_makes_no_api_calls_at_all(self):
        class StrictApi(FakeApi):
            def _any_call(self, *args):
                raise AssertionError("outbound touched the API")
            list_issues = _any_call
            get_issue = _any_call
            list_labels = _any_call
            create_issue = _any_call
            add_labels = _any_call
            remove_label = _any_call

        result = run_outbound(StrictApi(), self.params())
        self.assertEqual(0, result.created_issues + result.label_updates
                         + result.parked)

    def test_failure_on_one_task_is_logged_and_the_pass_continues(self):
        class FlakyApi(FakeApi):
            def list_labels(self, number):
                if number == 30:
                    raise RuntimeError("HTTP 500")
                return super().list_labels(number)

        self.file_task("pending", "broken_link", issue=30)
        self.file_task("pending", "healthy", issue=31)
        api = FlakyApi([_issue(30, "broken link"), _issue(31, "healthy")])
        result = run_outbound(api, self.params())
        self.assertEqual(1, result.label_updates)
        self.assertEqual({"snes-pending"}, self.carried(api, 31))
        self.assertTrue(any("outbound sync failed" in m
                            for m in self.messages))

    def test_sync_pass_reports_created_and_label_updates(self):
        self.file_task("pending", "brand_new", body="content")
        api = FakeApi()
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertEqual(1, report.created_issues)
        self.assertEqual(1, report.label_updates)
        self.assertIn("created_issues=1", report.summary_line())
        self.assertIn("label_updates=1", report.summary_line())


if __name__ == "__main__":
    unittest.main()
