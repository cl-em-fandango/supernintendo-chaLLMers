"""Slice 7 — the sync-pass engine: `sync_pass()`, `SyncReport`, summary, abort.

The engine composes the two phases into one observable pass (spec FR-3
core, NFR-4) and owns the mid-pass abort rule (edge 9, FR-5): a spent
rate-limit budget or an auth disable stops the remaining phase, the
counts gathered so far are reported, and nothing raises out of the CLI —
unfinished work rolls to the next pass.

Covered here:
  * a fake-API pass drives `sync_pass()` and observes imported + created
    + label updates with correct `SyncReport` counts;
  * a pass over an empty queue makes zero mutating calls;
  * a rate-limit fake aborts the pass mid-phase and mid-item, reporting
    it on the summary line instead of raising;
  * `harness sync` prints the one-line summary through the log sink;
  * disabled config prints the disabled message (Slice 1) and builds no
    GitHub client, so zero HTTP is possible.
All tests run in-process: temp dirs and a fake API injected through the
composition boundary (NFR-5).
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

from external.github_api import (  # noqa: E402
    GitHubRateLimitError, Issue, IssueState, Label,
)
from harness.cli import handlers  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.sync import SyncReport, sync_pass  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    SyncLinkage, file_sidecar_path, write_legacy_linkage,
)

REPO = "acme/widgets"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


def _issue(number, title, labels=("snes",), state=IssueState.OPEN,
           body="issue body"):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The read/mutating surface a full pass uses; mutations recorded.

    `fail_reads` / `fail_mutations` name requests that raise a spent
    rate limit (edge 9) instead of answering.
    """

    def __init__(self, issues=(), labels=None, fail_reads=(),
                 fail_mutations=()):
        self.issues = {issue.number: issue for issue in issues}
        self.labels = {number: list(names)
                       for number, names in (labels or {}).items()}
        self.mutations = []
        self.reads = []
        self.fail_reads = set(fail_reads)
        self.fail_mutations = set(fail_mutations)

    # -- reads -------------------------------------------------------------

    def _read(self, key):
        self.reads.append(key)
        if key[0] in self.fail_reads:
            raise GitHubRateLimitError("github rate limit exceeded: spent")

    def list_issues(self, labels=(), state=IssueState.OPEN):
        self._read(("list_issues", tuple(labels), state))
        wanted = set(labels)
        return [issue for number, issue in sorted(self.issues.items())
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]

    def get_issue(self, number):
        self._read(("get_issue", number))
        return self.issues[number]

    def list_labels(self, number):
        self._read(("list_labels", number))
        return [Label(name) for name in self.labels.get(number, [])]

    # -- mutations -----------------------------------------------------------

    def _mutate(self, record):
        self.mutations.append(record)
        if record[0] in self.fail_mutations:
            raise GitHubRateLimitError("github rate limit exceeded: spent")

    def create_issue(self, title, body):
        self._mutate(("create", title, body))
        number = max(self.issues, default=0) + 1
        self.issues[number] = _issue(number, title, labels=(), body=body)
        self.labels.setdefault(number, [])
        return self.issues[number]

    def add_labels(self, number, labels):
        self._mutate(("add", number, tuple(labels)))
        carried = self.labels.setdefault(number, [])
        self.labels[number] = carried + [n for n in labels if n not in carried]
        return [Label(n) for n in self.labels[number]]

    def remove_label(self, number, name):
        self._mutate(("remove", number, name))
        self.labels[number] = [n for n in self.labels.get(number, [])
                               if n != name]

    def close_issue(self, number):
        self._mutate(("close", number))
        issue = self.issues[number]
        self.issues[number] = _issue(number, issue.title,
                                     labels=tuple(l.name for l in issue.labels),
                                     state=IssueState.CLOSED, body=issue.body)
        return self.issues[number]


class SyncPassTestCase(unittest.TestCase):
    """`sync_pass()` against temp dirs + fake API through the cfg."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "harnessExecutionAndQueueDir": str(self.work_dir), "githubPat": "ghp_token",
            "githubRepo": REPO}))
        self.cfg = load(cfg_path)
        self.messages = []

    def file_task(self, name, location="pending", issue=None):
        path = self.queue / location / f"{name}.md"
        path.write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    def test_full_pass_reports_imported_created_and_label_updates(self):
        # #7 is an ingest trigger already carrying its target state label,
        # so it imports without a label update; `legacy_task` has no issue
        # at all, so outbound creates one and labels it.
        api = FakeApi(issues=[_issue(7, "Sync feature",
                                     labels=("snes", "snes-pending"))],
                      labels={7: ["snes", "snes-pending"]})
        self.file_task("legacy_task")
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertEqual(1, report.imported)
        self.assertEqual(1, report.created_issues)
        self.assertEqual(1, report.label_updates)
        self.assertFalse(report.aborted)
        summary = report.summary_line()
        for part in ("imported=1", "created_issues=1", "label_updates=1"):
            self.assertIn(part, summary)
        self.assertTrue((self.queue / "pending" / "Sync_feature.md").is_file())
        self.assertIn(("add", 8, ("snes-pending",)), api.mutations)

    def test_empty_queue_makes_zero_mutating_calls(self):
        api = FakeApi()
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertEqual(SyncReport(), report)
        self.assertEqual([], api.mutations)
        self.assertIn("github sync: imported=0", report.summary_line())

    def test_rate_limit_mid_pass_aborts_and_rolls_to_next_pass(self):
        """Edge 9: inbound work is kept, the outbound phase never starts,
        and the pass reports instead of raising."""
        api = FakeApi(issues=[_issue(7, "Sync feature",
                                     labels=("snes", "snes-pending"))],
                      labels={7: ["snes", "snes-pending"]})
        original = api.list_issues

        def flaky_list(labels=(), state=IssueState.OPEN):
            if not labels:  # the outbound open/closed listings
                raise GitHubRateLimitError("github rate limit exceeded")
            return original(labels=labels, state=state)

        api.list_issues = flaky_list
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertEqual(1, report.imported)
        self.assertEqual(0, report.created_issues)
        self.assertTrue(report.aborted)
        self.assertIn("ABORTED", report.summary_line())
        self.assertTrue(any("pass aborted" in m for m in self.messages))

    def test_rate_limit_stops_remaining_items_in_the_phase(self):
        """A spent budget mid-loop aborts the phase: task two is never
        touched, and the error surfaces as an abort, not a per-item skip."""
        api = FakeApi(issues=[_issue(1, "First task"), _issue(2, "Second task")],
                      labels={1: ["snes"], 2: ["snes"]},
                      fail_mutations=("add",))
        self.file_task("first_task", issue=1)
        self.file_task("second_task", issue=2)
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertTrue(report.aborted)
        self.assertEqual(0, report.label_updates)
        self.assertEqual([("add", 1, ("snes-pending",))], api.mutations)

    def test_ordinary_failures_stay_per_item(self):
        """NFR-1 regression: a non-abort error skips one task, the pass
        finishes healthy and is not marked aborted."""
        api = FakeApi(issues=[_issue(1, "First task"), _issue(2, "Second task")],
                      labels={1: ["snes"], 2: ["snes"]})

        def boom(number):
            raise RuntimeError("transport exploded")

        original = api.list_labels
        api.list_labels = lambda number: (
            boom(number) if number == 1 else original(number))
        self.file_task("first_task", issue=1)
        self.file_task("second_task", issue=2)
        report = sync_pass(self.cfg, api, log=self.messages.append)
        self.assertFalse(report.aborted)
        self.assertEqual(1, report.label_updates)


class CmdSyncSummaryTest(unittest.TestCase):
    """`harness sync` end-to-end: summary line, abort exit code, disabled."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

    def _run_sync(self, raw: dict, api=None) -> tuple[int, str]:
        cfg_path = self.work / "config.json"
        cfg_path.write_text(json.dumps({"harnessExecutionAndQueueDir": str(self.work), **raw}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            factory = (lambda cfg, log=None: api) if api is not None \
                else mock.Mock(side_effect=AssertionError("HTTP built"))
            with mock.patch.object(handlers, "build_github_api", factory):
                with contextlib.redirect_stdout(out):
                    rc = handlers.cmd_sync()
        return rc, out.getvalue()

    def test_enabled_sync_prints_the_summary_line(self):
        rc, out = self._run_sync({"githubPat": "ghp_token",
                                  "githubRepo": REPO}, api=FakeApi())
        self.assertEqual(0, rc)
        self.assertIn("github sync: imported=0 parked=0 deleted=0 "
                      "created_issues=0 label_updates=0 "
                      "comments_posted=0", out)

    def test_aborted_pass_reports_and_exits_zero(self):
        """Edge 9 through the CLI: the abort is a reported summary, not
        an exception or a failed exit code."""
        api = FakeApi(fail_reads=("list_issues",))
        rc, out = self._run_sync({"githubPat": "ghp_token",
                                  "githubRepo": REPO}, api=api)
        self.assertEqual(0, rc)
        self.assertIn("ABORTED", out)
        self.assertEqual([], api.mutations)

    def test_disabled_config_prints_disabled_and_builds_no_api(self):
        rc, out = self._run_sync({})
        self.assertEqual(0, rc)
        self.assertIn("github sync disabled", out)
        self.assertNotIn("github sync:", out)


if __name__ == "__main__":
    unittest.main()
