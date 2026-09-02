"""Slice 4 — inbound halt: `snes-parked` / `snes-deleted` stop and move a task.

Covers spec FR-1.3 (park from any queue location), FR-1.4 (delete plus the
close-and-unlabel anti-loop), FR-1.5 (one action per issue, delete > park >
ingest) and the in-flight rule of edge case 6 (stand-down flag at the session
boundary; the move happens only after the session exited — never a kill),
plus AC-3 and AC-4. All tests run in-process: temp queue directories, a
fake API object and the real `TaskLifecycle` (NFR-5). The stand-down
boundary is faked with the real `StandDownWatcher`, the same check the run
loops call.

Covered here:
  * AC-3: a `snes-parked` issue parks its task from `pending/` (status
    stamp + executive summary citing the issue number, sidecar relocated);
    an already-parked task is a silent no-op;
  * park works from `claimed/`, `review/`, `done/`, `failed/` too, and for
    closed issues;
  * in-flight park: pass 1 only sets the stand-down flag, the task stays
    in `active/` while the request is pending, and only after a faked
    session boundary (ack -> paused) does the next pass move it;
  * AC-4: `snes-deleted` removes the task file (or the whole `active/`
    dir, after the boundary), closes the issue and removes `snes` +
    `snes-deleted`; a closed issue is not re-closed; no match is a no-op;
  * precedence: delete > park > ingest;
  * a GitHub-side failure on one issue is logged and skipped; the pass
    keeps going (NFR-1);
  * `sync_pass()` reports parked/deleted counts.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Issue, IssueState, Label  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.interrupt import (  # noqa: E402
    InterruptMode,
    InterruptState,
    read_interrupt,
)
from harness.core.stand_down import StandDownWatcher  # noqa: E402
from harness.core.sync import sync_pass  # noqa: E402
from harness.core.sync_inbound import InboundParams, run_inbound  # noqa: E402
from harness.core import task_record  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    file_sidecar_path,
    write_legacy_linkage,
    SyncLinkage,
)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels, state=IssueState.OPEN):
    return Issue(number=number, title=title, body="body", state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """Read/mutating surface the halt paths use, recorded for assertions."""

    def __init__(self, issues):
        self.issues = {issue.number: issue for issue in issues}
        self.closed = []
        self.removed_labels = []

    def list_issues(self, labels=(), state=IssueState.OPEN):
        wanted = set(labels)
        return [issue for number, issue in sorted(self.issues.items())
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def close_issue(self, number):
        self.closed.append(number)
        self.issues[number] = replace(self.issues[number],
                                      state=IssueState.CLOSED)

    def remove_label(self, number, name):
        self.removed_labels.append((number, name))
        issue = self.issues[number]
        self.issues[number] = replace(
            issue, labels=tuple(label for label in issue.labels
                                if label.name != name))


class HaltTestCase(unittest.TestCase):
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
        return InboundParams(queue_dir=self.queue, repo=REPO,
                             log=self.messages.append,
                             work_dir=self.work_dir,
                             lifecycle=TaskLifecycle(self.cfg,
                                                     log=self.messages.append))

    def file_task(self, location, name, issue=None):
        """A bare task file, optionally linked to `issue` by sidecar."""
        path = self.queue / location / f"{name}.md"
        path.write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    def dir_task(self, location, name, issue=None):
        """A task directory with a minimal `task.json` and optional sidecar."""
        task_dir = self.queue / location / name
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(task_dir / "gh.json",
                          SyncLinkage(issue=issue, repo=REPO))
        return task_dir

    def crossed_boundary(self):
        """The fake session boundary: the run loop's stand-down check
        acknowledges the request (requested -> paused) and the session exits."""
        watcher = StandDownWatcher(work_dir=self.work_dir,
                                   log=self.messages.append)
        self.assertTrue(watcher(), "expected the stand-down request to be "
                                   "acknowledged at the boundary")

    def parked_dir(self, name):
        return self.queue / "parked" / name

    def review_summary(self, name):
        return (self.queue / "review" / f"{name}.md").read_text()


class ParkTest(HaltTestCase):
    def test_ac3_pending_task_parked_with_summary_and_linkage(self):
        task = self.file_task("pending", "task_a", issue=7)
        api = FakeApi([_issue(7, "Task a", ["snes", "snes-parked"])])
        result = run_inbound(api, self.params())
        self.assertEqual(1, result.parked)
        self.assertFalse(task.exists())
        parked = self.parked_dir("task_a")
        self.assertEqual("# task_a body",
                         (parked / "original.md").read_text())
        state = json.loads((parked / "task.json").read_text())
        self.assertEqual("parked", state["status"])
        self.assertIn("parked via GitHub issue #7",
                      self.review_summary("task_a"))
        # The linkage needs no move: the record is keyed by task id, and the
        # legacy sidecar the seed left behind is retired by the migration.
        self.assertFalse(file_sidecar_path(task).exists())
        self.assertFalse((parked / "gh.json").exists())
        self.assertEqual(7, task_record.read_linkage(self.queue, "task_a")
                         .issue)
        # Idempotent: a second pass sees the parked record match, no-op.
        before = sorted(str(p) for p in self.queue.rglob("*"))
        self.assertEqual(0, run_inbound(api, self.params()).parked)
        self.assertEqual(before, sorted(str(p) for p in self.queue.rglob("*")))

    def test_in_flight_task_stands_down_then_parks_after_boundary(self):
        task_dir = self.dir_task("active", "task_b", issue=8)
        api = FakeApi([_issue(8, "Task b", ["snes-parked"])])
        result = run_inbound(api, self.params())
        # Pass 1: only the flag. The session is mid-request; nothing moves.
        self.assertEqual(0, result.parked)
        self.assertTrue(task_dir.is_dir())
        self.assertFalse(self.parked_dir("task_b").exists())
        status = read_interrupt(self.work_dir)
        self.assertIs(InterruptMode.STAND_DOWN, status.mode)
        self.assertIs(InterruptState.REQUESTED, status.state)
        # Still pending: another pass before the boundary changes nothing.
        self.assertEqual(0, run_inbound(api, self.params()).parked)
        self.assertTrue(task_dir.is_dir())
        # The fake session exits at its boundary (ack -> paused) ...
        self.crossed_boundary()
        # ... the next pass moves it through the park path.
        self.assertEqual(1, run_inbound(api, self.params()).parked)
        self.assertFalse(task_dir.exists())
        state = json.loads(
            (self.parked_dir("task_b") / "task.json").read_text())
        self.assertEqual("parked", state["status"])
        self.assertIn("parked via GitHub issue #8",
                      self.review_summary("task_b"))

    def test_already_parked_is_a_silent_noop(self):
        parked = self.dir_task("parked", "task_c", issue=9)
        api = FakeApi([_issue(9, "Task c", ["snes-parked"])])
        result = run_inbound(api, self.params())
        self.assertEqual(0, result.parked)
        self.assertTrue(parked.is_dir())
        self.assertEqual([], api.closed)
        self.assertEqual([], api.removed_labels)
        self.assertTrue(any("skip" in m and "already parked" in m
                            for m in self.messages))

    def test_no_matching_task_is_a_noop(self):
        api = FakeApi([_issue(70, "Nothing here", ["snes-parked"])])
        self.assertEqual(0, run_inbound(api, self.params()).parked)
        self.assertEqual([], api.closed)
        self.assertEqual([], api.removed_labels)

    def test_closed_issue_parks_too(self):
        self.file_task("pending", "task_d", issue=11)
        api = FakeApi([_issue(11, "Task d", ["snes-parked"],
                              state=IssueState.CLOSED)])
        self.assertEqual(1, run_inbound(api, self.params()).parked)
        self.assertTrue(self.parked_dir("task_d").is_dir())

    def test_park_from_every_other_location(self):
        for offset, location in enumerate(("claimed", "review",
                                           "done", "failed")):
            with self.subTest(location=location):
                number = 12 + offset  # one issue per task, no sidecar reuse
                if location in ("done", "failed"):
                    self.dir_task(location, f"task_{location}", issue=number)
                else:
                    self.file_task(location, f"task_{location}", issue=number)
                api = FakeApi([_issue(number, f"Task {location}",
                                      ["snes-parked"])])
                self.assertEqual(1, run_inbound(api, self.params()).parked)
                self.assertTrue(
                    self.parked_dir(f"task_{location}").is_dir())


class DeleteTest(HaltTestCase):
    def test_ac4_pending_file_deleted_issue_closed_and_unlabeled(self):
        task = self.file_task("pending", "gone", issue=10)
        api = FakeApi([_issue(10, "Gone", ["snes", "snes-deleted"])])
        result = run_inbound(api, self.params())
        self.assertEqual(1, result.deleted)
        self.assertFalse(task.exists())
        self.assertFalse(file_sidecar_path(task).exists())
        self.assertEqual([10], api.closed)
        self.assertEqual({(10, "snes"), (10, "snes-deleted")},
                         set(api.removed_labels))
        # Anti-loop: the closed, un-labeled issue imports nothing next pass.
        quiet = run_inbound(api, self.params())
        self.assertEqual(0, quiet.deleted + quiet.imported + quiet.parked)

    def test_in_flight_dir_deleted_only_after_boundary(self):
        task_dir = self.dir_task("active", "gone_live", issue=13)
        api = FakeApi([_issue(13, "Gone live", ["snes-deleted"])])
        self.assertEqual(0, run_inbound(api, self.params()).deleted)
        self.assertTrue(task_dir.is_dir())
        self.assertEqual([], api.closed)
        self.crossed_boundary()
        self.assertEqual(1, run_inbound(api, self.params()).deleted)
        self.assertFalse(task_dir.exists())
        self.assertEqual([13], api.closed)

    def test_closed_issue_is_not_reclosed(self):
        self.file_task("pending", "old", issue=14)
        api = FakeApi([_issue(14, "Old", ["snes", "snes-deleted"],
                              state=IssueState.CLOSED)])
        self.assertEqual(1, run_inbound(api, self.params()).deleted)
        self.assertEqual([], api.closed)
        self.assertEqual({(14, "snes"), (14, "snes-deleted")},
                         set(api.removed_labels))

    def test_api_failure_on_one_issue_is_logged_and_the_pass_continues(self):
        class FlakyApi(FakeApi):
            def close_issue(self, number):
                raise RuntimeError("HTTP 500")

        self.file_task("pending", "drop", issue=17)
        self.file_task("pending", "keep", issue=18)
        api = FlakyApi([_issue(17, "Drop", ["snes-deleted"]),
                        _issue(18, "Keep", ["snes-parked"])])
        result = run_inbound(api, self.params())
        # The failing close does not abort the pass: #18 still parks, the
        # errored issue is not tallied, and its task is already gone.
        self.assertEqual(0, result.deleted)
        self.assertEqual(1, result.parked)
        self.assertFalse((self.queue / "pending" / "drop.md").exists())
        self.assertTrue(any("action failed" in m for m in self.messages))

    def test_no_matching_task_is_a_noop(self):
        api = FakeApi([_issue(71, "Never was", ["snes-deleted"])])
        self.assertEqual(0, run_inbound(api, self.params()).deleted)
        self.assertEqual([], api.closed)
        self.assertEqual([], api.removed_labels)


class PrecedenceTest(HaltTestCase):
    def test_delete_wins_over_park(self):
        self.file_task("pending", "both", issue=15)
        api = FakeApi([_issue(15, "Both",
                              ["snes", "snes-parked", "snes-deleted"])])
        result = run_inbound(api, self.params())
        self.assertEqual(1, result.deleted)
        self.assertEqual(0, result.parked)
        self.assertFalse(self.parked_dir("both").exists())
        self.assertFalse((self.queue / "pending" / "both.md").exists())

    def test_park_wins_over_ingest(self):
        self.file_task("pending", "existing", issue=16)
        api = FakeApi([_issue(16, "Existing", ["snes", "snes-parked"])])
        result = run_inbound(api, self.params())
        # Park applies (ingest must not fire even though `snes` is present).
        self.assertEqual(1, result.parked)
        self.assertEqual(0, result.imported)
        self.assertEqual([], list((self.queue / "pending").glob("*.md")))


class SyncPassReportTest(HaltTestCase):
    def test_sync_pass_reports_parked_and_deleted(self):
        self.file_task("pending", "keep", issue=20)
        self.file_task("pending", "drop", issue=21)
        api = FakeApi([_issue(20, "Keep", ["snes-parked"]),
                       _issue(21, "Drop", ["snes-deleted"])])
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertEqual(1, report.parked)
        self.assertEqual(1, report.deleted)
        self.assertIn("parked=1", report.summary_line())
        self.assertIn("deleted=1", report.summary_line())


if __name__ == "__main__":
    unittest.main()
