"""Slice 3 — the record survives every queue transition (FR-C, §5.3, §5.4).

The record lives at `<queue>/.meta/<task-id>.json`, keyed by the task id, so
no transition has to carry it. These tests prove that for the transitions the
spec enumerates (FR-C.2 intake, FR-C.3 terminal moves in both the dir and the
file→dir variant, FR-C.6 sync terminal parking) and for the two edge cases
this property is meant to fix:

  * §5.3 — a *file* task reaches a terminal location: the record is still
    resolvable for `done|parked/<id>/`, and a terminal task dir still wins
    the linkage over a same-named review summary file's legacy sidecar;
  * §5.4 — the staging markdown is released (deleted) while the metadata
    persists: linkage is still resolvable by task id, because the record, not
    the task file, is the linkage's home.

Also covered: FR-B3 (a record takes precedence over title matching, including
for a task in a terminal location), FR-E2 (a legacy in-dir `gh.json` is
adopted by id across a terminal move), and FR-0.1 (a GitHub-disabled sync
writes no record at all). Everything runs in-process on temp dirs.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Issue, IssueState, Label  # noqa: E402
from harness.cli import handlers  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.sync_inbound import (  # noqa: E402
    InboundParams,
    run_inbound,
)
from tests.legacy_sidecars import (  # noqa: E402
    SIDECAR_SUFFIX,
    TASK_DIR_SIDECAR_NAME,
    SyncLinkage,
    file_sidecar_path,
    write_legacy_linkage,
)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels=("snes",), state=IssueState.OPEN):
    return Issue(number=number, title=title, body="issue body", state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The inbound read surface: label/state-filtered issue listing."""

    def __init__(self, issues):
        self.issues = issues

    def list_issues(self, labels=(), state=IssueState.OPEN):
        wanted = set(labels)
        return [issue for issue in self.issues
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]


class TransitionTestCase(unittest.TestCase):
    """A temp queue root, a lifecycle over it, and record/legacy helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.queue = self.work / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work / "config.json"
        cfg_path.write_text(json.dumps({"harnessExecutionAndQueueDir": str(self.work),
                                        "targetCodebaseDir": str(self.work)}))
        self.cfg = load(cfg_path)
        self.messages: list[str] = []
        self.lifecycle = TaskLifecycle(self.cfg, log=self.messages.append)

    # -- seeding -----------------------------------------------------------

    def seed_file(self, location, name, body="# task body"):
        path = self.queue / location / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def seed_dir(self, location, name):
        task_dir = self.queue / location / name
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps(
            {"id": name, "status": "active"}))
        (task_dir / "original.md").write_text("# task body")
        return task_dir

    # -- assertions --------------------------------------------------------

    def assert_no_legacy_metadata(self):
        """No legacy sidecar shape exists anywhere under the queue root."""
        leftovers = [
            p for p in self.queue.rglob("*")
            if p.is_file() and (
                p.name.endswith(SIDECAR_SUFFIX)
                or p.name.endswith(".claim.json")
                or p.name == TASK_DIR_SIDECAR_NAME)
        ]
        self.assertEqual([], leftovers)


class IntakeTransitionTest(TransitionTestCase):
    """FR-C.2: `claimed/<id>.md` -> `active/<id>/`, staging file deleted."""

    def test_intake_keeps_both_sections_resolvable(self):
        self.seed_file("pending", "task_a")
        task_record.write_linkage(
            self.queue, "task_a",
            SyncLinkage(issue=3, repo=REPO, comment_ids={"1": "c"},
                        demo=True))
        provider = DirectoryTaskProvider(self.queue / "pending",
                                     self.queue / "claimed",
                                     log=self.messages.append)
        tasks = provider.fetch_pending(claim=True, owner="inv-1")
        self.assertEqual(1, len(tasks))
        self.assertEqual({"demo": True}, tasks[0].meta)
        task = tasks[0]

        task_dir = self.lifecycle.intake(task)
        self.assertEqual(self.queue / "active" / task.id, task_dir)
        # Intake copies the body into the task dir; the pipeline then removes
        # the staging file (§5.4: the markdown goes, the metadata stays).
        (self.queue / "claimed" / f"{task.id}.md").unlink()

        record = task_record.read_record(self.queue, task.id)
        self.assertIsNotNone(record.github)
        self.assertEqual(3, record.github.issue)
        self.assertEqual({"1": "c"}, record.github.comment_ids)
        self.assertTrue(record.github.demo)
        self.assertIsNotNone(record.claim)
        self.assertEqual("inv-1", record.claim.owner)
        self.assert_no_legacy_metadata()


class TerminalMoveTest(TransitionTestCase):
    """FR-C.3: terminal moves, dir variant and file→dir variant."""

    def test_dir_terminal_move_keeps_record_resolvable(self):
        self.seed_dir("active", "mover")
        task_record.write_linkage(
            self.queue, "mover", SyncLinkage(issue=5, repo=REPO))

        self.lifecycle.complete("mover", "all done")

        self.assertTrue((self.queue / "done" / "mover" / "original.md")
                        .is_file())
        linkage = task_record.read_linkage(self.queue, "mover")
        self.assertIsNotNone(linkage)
        self.assertEqual(5, linkage.issue)
        self.assertFalse((self.queue / "done" / "mover"
                          / TASK_DIR_SIDECAR_NAME).exists(),
                         "linkage was written into the terminal task dir")
        self.assert_no_legacy_metadata()

    def test_file_to_dir_terminal_move_keeps_record_resolvable(self):
        self.seed_file("pending", "late")
        task_record.write_linkage(
            self.queue, "late", SyncLinkage(issue=6, repo=REPO))

        self.lifecycle.park("late", "parked by hand", from_="pending")

        self.assertTrue((self.queue / "parked" / "late" / "original.md")
                        .is_file())
        self.assertEqual(6, task_record.read_linkage(self.queue, "late").issue)
        self.assert_no_legacy_metadata()

    def test_legacy_dir_sidecar_is_adopted_across_the_move(self):
        task_dir = self.seed_dir("active", "legacy")
        write_legacy_linkage(task_dir / TASK_DIR_SIDECAR_NAME,
                             SyncLinkage(issue=9, repo=REPO))

        self.lifecycle.fail("legacy", "kicked out")

        self.assertTrue((self.queue / "failed" / "legacy").is_dir())
        self.assertEqual(9,
                         task_record.read_linkage(self.queue, "legacy").issue)
        self.assert_no_legacy_metadata()

    def test_terminal_dir_linkage_wins_over_review_file_sidecar(self):
        """§5.3 precedence: the terminal dir shadows a same-named review file."""
        self.seed_dir("done", "both")
        write_legacy_linkage(self.queue / "done" / "both" / TASK_DIR_SIDECAR_NAME,
                             SyncLinkage(issue=9, repo=REPO))
        self.seed_file("review", "both", body="summary")
        write_legacy_linkage(
            file_sidecar_path(self.queue / "review" / "both.md"),
            SyncLinkage(issue=3, repo=REPO))

        self.assertEqual(9, task_record.read_linkage(self.queue, "both").issue)


class ReleasedMarkdownTest(TransitionTestCase):
    """§5.4: the metadata outlives the task file and stays resolvable."""

    def test_record_only_task_still_resolves_by_id(self):
        task_record.write_linkage(
            self.queue, "released", SyncLinkage(issue=12, repo=REPO))
        self.seed_file("pending", "released").unlink()

        self.assertEqual(12,
                         task_record.read_linkage(self.queue, "released").issue)

    def test_legacy_sidecar_of_a_gone_markdown_migrates_by_id(self):
        orphan = self.queue / "pending" / f"gone.md{SIDECAR_SUFFIX}"
        write_legacy_linkage(orphan, SyncLinkage(issue=11, repo=REPO))

        self.assertEqual(11, task_record.read_linkage(self.queue, "gone").issue)
        self.assertFalse(orphan.exists())
        self.assertTrue(
            task_record.record_path(self.queue, "gone").is_file())


class LinkagePrecedenceTest(TransitionTestCase):
    """FR-B3: a record beats title matching, terminal locations included."""

    def test_terminal_record_blocks_a_title_match_for_another_issue(self):
        self.seed_dir("done", "fix_the_parser")
        task_record.write_linkage(
            self.queue, "fix_the_parser", SyncLinkage(issue=5, repo=REPO))
        api = FakeApi([_issue(6, "fix the parser")])

        result = run_inbound(api, InboundParams(queue_dir=self.queue,
                                                repo=REPO,
                                                log=self.messages.append))

        self.assertEqual(1, result.imported)
        imported = sorted(p.name for p in (self.queue / "pending").iterdir())
        self.assertEqual(["fix_the_parser-6.md"], imported,
                         "the title must not steal the done task's issue")
        self.assertEqual(5,
                         task_record.read_linkage(
                             self.queue, "fix_the_parser").issue)

    def test_terminal_record_matches_its_own_issue(self):
        self.seed_dir("done", "old_task")
        task_record.write_linkage(
            self.queue, "old_task", SyncLinkage(issue=5, repo=REPO))
        api = FakeApi([_issue(5, "a completely different title")])

        self.assertEqual(0, run_inbound(api, InboundParams(
            queue_dir=self.queue, repo=REPO,
            log=self.messages.append)).imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))


class SyncParkingTest(TransitionTestCase):
    """FR-C.6: inbound terminal parking needs no metadata move."""

    def test_inbound_park_keeps_the_record_with_the_task(self):
        self.seed_file("pending", "stay")
        task_record.write_linkage(
            self.queue, "stay", SyncLinkage(issue=4, repo=REPO))
        api = FakeApi([_issue(4, "stay", labels=("snes", "snes-parked"))])

        result = run_inbound(api, InboundParams(
            queue_dir=self.queue, repo=REPO,
            log=self.messages.append, lifecycle=self.lifecycle))

        self.assertEqual(1, result.parked)
        self.assertTrue((self.queue / "parked" / "stay" / "original.md")
                        .is_file())
        self.assertEqual(4, task_record.read_linkage(self.queue, "stay").issue)
        self.assert_no_legacy_metadata()


class DisabledSyncTest(TransitionTestCase):
    """FR-0.1: a disabled GitHub sync writes no record from sync paths."""

    def test_disabled_sync_creates_no_meta_store(self):
        self.seed_file("pending", "untouched")
        cfg_path = self.work / "config.json"
        cfg_path.write_text(json.dumps({"harnessExecutionAndQueueDir": str(self.work)}))
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rc = handlers.cmd_sync()

        self.assertEqual(0, rc)
        self.assertIn("github sync disabled", out.getvalue())
        self.assertFalse((self.queue / task_record.META_DIR_NAME).exists())
        self.assertEqual("# task body",
                         (self.queue / "pending" / "untouched.md")
                         .read_text())


if __name__ == "__main__":
    unittest.main()
