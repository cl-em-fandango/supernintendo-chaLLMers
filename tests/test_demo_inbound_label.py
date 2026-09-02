"""Slice 2 — inbound `snes-demo` label -> pending task with a demo flag.

Covers demo spec FR-1.1–FR-1.7, edge cases 1–2 and AC-1:

  * an issue carrying only `snes-demo` ingests exactly once as a pending
    task whose sidecar reads `{"repo":…, "issue":…, "demo": true}`; a
    second inbound pass adds nothing (idempotent);
  * `snes` + `snes-demo` -> one task, flagged;
  * precedence delete > park > demo > ingest (FR-1.5);
  * `demo.enabled = false` -> `snes-demo` is ignored entirely: no listing,
    no task, no label changes (FR-9);
  * label added to an already-synced bare-`snes` issue flags the existing
    sidecar without duplicating the task (edge 1); a label removed later
    never un-flags an already-flagged task (edge 2);
  * the delete anti-loop removes `snes-demo` exactly where it removes
    `snes` (FR-1.6);
  * outbound sync never removes `snes-demo` (it is not a state label).

All tests run in-process: temp queue directories, fake API objects, an
injected GitHub transport; no network, no container (spec §6).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import Issue, IssueState, Label  # noqa: E402
from harness.core.sync_inbound import InboundParams, run_inbound  # noqa: E402
from harness.core.sync_labels import (  # noqa: E402
    TriggerLabel,
    is_harness_label,
    is_state_label,
)
from harness.core.sync_outbound import OutboundParams, run_outbound  # noqa: E402
from harness.core import task_record  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    file_sidecar_path,
    write_legacy_linkage,
    SyncLinkage,
)

REPO = "acme/widgets"
DEMO = TriggerLabel.DEMO.value


def _issue(number, title, body="# hello", labels=("snes-demo",),
           state=IssueState.OPEN):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """Label/state-filtered listing plus the write surface the delete
    anti-loop and outbound state-label diff use. Every write is recorded."""

    def __init__(self, issues):
        self.issues = {issue.number: issue for issue in issues}
        self.list_calls = []
        self.removed_labels = []
        self.added_labels = []
        self.closed = []

    def list_issues(self, labels=(), state=IssueState.OPEN):
        self.list_calls.append((tuple(labels), state))
        wanted = set(labels)
        return [issue for issue in sorted(self.issues.values(),
                                          key=lambda issue: issue.number)
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def list_labels(self, number):
        return [Label(name) for name in
                {label.name for label in self.issues[number].labels}]

    def add_labels(self, number, names):
        self.added_labels.append((number, list(names)))

    def remove_label(self, number, name):
        self.removed_labels.append((number, name))

    def close_issue(self, number):
        self.closed.append(number)
        issue = self.issues[number]
        self.issues[number] = Issue(number=issue.number, title=issue.title,
                                    body=issue.body, state=IssueState.CLOSED,
                                    labels=issue.labels,
                                    html_url=issue.html_url)


class DemoInboundTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "queue"
        for sub in ("pending", "claimed", "active", "review",
                    "parked", "failed", "done"):
            (self.queue / sub).mkdir(parents=True)
        self.messages = []

    def params(self, demo_enabled=True):
        return InboundParams(queue_dir=self.queue, repo=REPO,
                             log=self.messages.append,
                             demo_enabled=demo_enabled)

    def run_pass(self, api, demo_enabled=True):
        return run_inbound(api, self.params(demo_enabled=demo_enabled))

    def pending(self, name):
        return self.queue / "pending" / name

    def linkage_payload(self, name):
        """The task's recorded `github` section, as written on disk."""
        payload = json.loads(
            task_record.record_path(self.queue, Path(name).stem).read_text())
        return payload["github"]


class DemoIngestTest(DemoInboundTestCase):
    def test_demo_only_issue_ingests_flagged_task(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,))])
        self.assertEqual(1, self.run_pass(api).imported)
        task = self.pending("Pizza_fan_site.md")
        self.assertTrue(task.is_file())
        self.assertEqual("# hello", task.read_text())
        linkage = task_record.read_linkage(self.queue, task.stem)
        self.assertIsNotNone(linkage)
        self.assertEqual(7, linkage.issue)
        self.assertEqual(REPO, linkage.repo)
        self.assertTrue(linkage.demo)
        payload = self.linkage_payload("Pizza_fan_site.md")
        self.assertIs(True, payload["demo"])
        self.assertEqual(7, payload["issue"])
        self.assertEqual(REPO, payload["repo"])

    def test_second_pass_is_idempotent(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,))])
        self.assertEqual(1, self.run_pass(api).imported)
        before = sorted(p.name for p in self.queue.rglob("*"))
        self.assertEqual(0, self.run_pass(api).imported)
        self.assertEqual(before, sorted(p.name for p in self.queue.rglob("*")))
        self.assertIs(True,
                      self.linkage_payload("Pizza_fan_site.md")["demo"])

    def test_snes_plus_demo_ingests_one_flagged_task(self):
        api = FakeApi([_issue(8, "Pizza fan site",
                              labels=("snes", DEMO))])
        self.assertEqual(1, self.run_pass(api).imported)
        self.assertEqual([], list((self.queue / "pending").glob("*-8.md")))
        self.assertIs(True,
                      self.linkage_payload("Pizza_fan_site.md")["demo"])

    def test_plain_snes_issue_is_not_flagged(self):
        api = FakeApi([_issue(9, "Ordinary task", labels=("snes",))])
        self.assertEqual(1, self.run_pass(api).imported)
        payload = self.linkage_payload("Ordinary_task.md")
        self.assertIs(False, payload["demo"])
        self.assertFalse(
            task_record.read_linkage(self.queue, "Ordinary_task").demo)

    def test_demo_listing_only_when_enabled(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,))])
        self.run_pass(api, demo_enabled=True)
        self.assertIn(((DEMO,), IssueState.OPEN), api.list_calls)

    def test_closed_demo_issue_does_not_ingest(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,),
                              state=IssueState.CLOSED)])
        self.assertEqual(0, self.run_pass(api).imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))


class DemoPrecedenceTest(DemoInboundTestCase):
    def test_park_beats_demo(self):
        api = FakeApi([_issue(7, "Pizza fan site",
                              labels=(DEMO, "snes-parked"))])
        result = self.run_pass(api)
        self.assertEqual((0, 0), (result.imported, result.parked))
        self.assertEqual([], list((self.queue / "pending").iterdir()))

    def test_delete_beats_demo(self):
        api = FakeApi([_issue(7, "Pizza fan site",
                              labels=(DEMO, "snes-deleted"))])
        result = self.run_pass(api)
        self.assertEqual((0, 0), (result.imported, result.deleted))
        self.assertEqual([], list((self.queue / "pending").iterdir()))

    def test_demo_beats_ingest_flag_written(self):
        # `snes` + `snes-demo` resolves to the DEMO action, not INGEST:
        # the task lands flagged (FR-1.5 precedence).
        api = FakeApi([_issue(7, "Pizza fan site", labels=("snes", DEMO))])
        self.assertEqual(1, self.run_pass(api).imported)
        self.assertIs(True, self.linkage_payload("Pizza_fan_site.md")["demo"])


class DemoDisabledTest(DemoInboundTestCase):
    def test_disabled_ignores_label_entirely(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,))])
        result = self.run_pass(api, demo_enabled=False)
        self.assertEqual(0, result.imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))
        # No listing for the label, no label writes of any kind.
        self.assertEqual([], [call for call in api.list_calls
                              if DEMO in call[0]])
        self.assertEqual([], api.removed_labels)
        self.assertEqual([], api.added_labels)
        self.assertEqual([], api.closed)

    def test_disabled_still_ingests_bare_snes_unflagged(self):
        api = FakeApi([_issue(8, "Pizza fan site", labels=("snes", DEMO))])
        self.assertEqual(1, self.run_pass(api, demo_enabled=False).imported)
        self.assertIs(False, self.linkage_payload("Pizza_fan_site.md")["demo"])


class DemoFlagExistingTaskTest(DemoInboundTestCase):
    def test_label_added_after_ingest_flags_without_duplicate(self):
        # Pass 1: bare `snes` issue ingests unflagged.
        issue = _issue(7, "Pizza fan site", labels=("snes",))
        api = FakeApi([issue])
        self.assertEqual(1, self.run_pass(api).imported)
        task = self.pending("Pizza_fan_site.md")
        self.assertIs(False, self.linkage_payload("Pizza_fan_site.md")["demo"])
        # Pass 2: the label was added on GitHub; the existing linkage is
        # flagged and no second task appears (edge 1).
        api.issues[7] = _issue(7, "Pizza fan site", labels=("snes", DEMO))
        result = self.run_pass(api)
        self.assertEqual(0, result.imported)
        self.assertEqual([task.name],
                         [p.name for p in (self.queue / "pending").iterdir()
                          if p.suffix == ".md"])
        self.assertIs(True, self.linkage_payload("Pizza_fan_site.md")["demo"])

    def test_flag_survives_task_dir_sidecar(self):
        task_dir = self.queue / "active" / "Pizza_fan_site"
        task_dir.mkdir()
        write_legacy_linkage(task_dir / "gh.json",
                      SyncLinkage(issue=7, repo=REPO))
        api = FakeApi([_issue(7, "Whatever title", labels=(DEMO,))])
        self.assertEqual(0, self.run_pass(api).imported)
        # The legacy task-dir sidecar is adopted by the record, which is
        # where the flag is written and read from now on.
        self.assertFalse((task_dir / "gh.json").exists())
        payload = json.loads(
            task_record.record_path(self.queue, "Pizza_fan_site")
            .read_text())["github"]
        self.assertIs(True, payload["demo"])
        self.assertEqual(7, payload["issue"])

    def test_unflagged_title_match_gets_linked_and_flagged(self):
        (self.queue / "pending" / "Pizza_fan_site.md").write_text("manual")
        api = FakeApi([_issue(7, "Pizza fan site", labels=(DEMO,))])
        self.assertEqual(0, self.run_pass(api).imported)
        payload = self.linkage_payload("Pizza_fan_site.md")
        self.assertIs(True, payload["demo"])
        self.assertEqual(7, payload["issue"])

    def test_label_removed_later_keeps_flag(self):
        # Edge 2: no un-flagging within the current cycle.
        task = self.pending("Pizza_fan_site.md")
        task.write_text("# hello")
        write_legacy_linkage(file_sidecar_path(task),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        api = FakeApi([_issue(7, "Pizza fan site", labels=("snes",))])
        self.run_pass(api)
        self.assertIs(True, self.linkage_payload("Pizza_fan_site.md")["demo"])


class DemoDeleteAntiLoopTest(DemoInboundTestCase):
    def test_delete_removes_demo_where_it_removes_snes(self):
        task = self.pending("Pizza_fan_site.md")
        task.write_text("# hello")
        write_legacy_linkage(file_sidecar_path(task),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        api = FakeApi([_issue(7, "Pizza fan site",
                              labels=("snes", DEMO, "snes-deleted",
                                      "human-label"))])
        self.assertEqual(1, self.run_pass(api).deleted)
        self.assertFalse(task.exists())
        self.assertEqual(7, api.closed[0])
        removed = {name for _, name in api.removed_labels}
        self.assertEqual({"snes", "snes-deleted", DEMO}, removed)
        self.assertNotIn("human-label", removed)

    def test_delete_disabled_leaves_demo_label_alone(self):
        task = self.pending("Pizza_fan_site.md")
        task.write_text("# hello")
        write_legacy_linkage(file_sidecar_path(task),
                      SyncLinkage(issue=7, repo=REPO))
        api = FakeApi([_issue(7, "Pizza fan site",
                              labels=("snes", DEMO, "snes-deleted"))])
        self.assertEqual(1, self.run_pass(api, demo_enabled=False).deleted)
        removed = {name for _, name in api.removed_labels}
        self.assertEqual({"snes", "snes-deleted"}, removed)


class DemoOutboundMarkerTest(DemoInboundTestCase):
    def test_snes_demo_is_harness_owned_but_not_a_state_label(self):
        self.assertTrue(is_harness_label(DEMO))
        self.assertFalse(is_state_label(DEMO))

    def test_outbound_never_removes_demo(self):
        task = self.pending("Pizza_fan_site.md")
        task.write_text("# hello")
        write_legacy_linkage(file_sidecar_path(task),
                      SyncLinkage(issue=7, repo=REPO, demo=True))
        api = FakeApi([_issue(7, "Pizza fan site",
                              labels=("snes", DEMO, "snes-active",
                                      "human-label"))])
        result = run_outbound(api, OutboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append))
        self.assertEqual(1, result.label_updates)
        self.assertEqual([(7, ["snes-pending"])], api.added_labels)
        self.assertEqual([(7, "snes-active")], api.removed_labels)


if __name__ == "__main__":
    unittest.main()
