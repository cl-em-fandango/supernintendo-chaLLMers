"""Slice 3 — inbound import: `snes` issues become pending tasks.

Covers spec FR-1.1 (filename derivation), FR-1.2 (ingest), FR-1.6 (the
`gh.json` sidecar linkage) and FR-1.7 (idempotency), plus AC-2. All tests
run in-process: temp queue directories and a fake API object; no network,
no container (NFR-5).

Covered here:
  * AC-2: open issue `Test sync feature` (label `snes`, body `# hello`) ->
    `pending/Test_sync_feature.md` with that body; a second pass changes
    nothing and imports nothing;
  * empty body -> title+URL stub; blank title -> `<number>.md` fallback;
    200-char stem cap; `/` stripped;
  * collision suffix `-<number>` when a same-named file belongs to another
    issue (sidecar wins over title, FR-1.6); overwrite-skip;
  * sidecar `{"issue", "repo"}` written on import; sidecar match in any
    location (pending file or active task dir) prevents re-import;
  * only open issues carrying the bare `snes` label are considered;
  * `sync_pass()` reports the import count and `harness sync` (enabled,
    fake API injected) prints the summary line.
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
from harness.core.sync import SyncReport, sync_pass  # noqa: E402
from harness.core.sync_inbound import (  # noqa: E402
    MAX_STEM_CHARS,
    InboundParams,
    derive_task_filename,
    find_matching_task,
    run_inbound,
    scan_queue,
)
from harness.core.sync_sidecar import (  # noqa: E402
    file_sidecar_path,
    read_linkage,
    write_linkage,
    SyncLinkage,
)

REPO = "acme/widgets"


def _issue(number, title, body="# hello", labels=("snes",),
           state=IssueState.OPEN, url=None):
    return Issue(number=number, title=title, body=body, state=state,
                 labels=tuple(Label(name) for name in labels),
                 html_url=url or f"https://github.com/{REPO}/issues/{number}")


class FakeApi:
    """The slice-3 read surface: label/state-filtered issue listing."""

    def __init__(self, issues):
        self.issues = issues
        self.list_calls = []

    def list_issues(self, labels=(), state=IssueState.OPEN):
        self.list_calls.append((tuple(labels), state))
        wanted = set(labels)
        return [issue for issue in self.issues
                if wanted <= {label.name for label in issue.labels}
                and issue.state == state]


class InboundTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.queue = Path(self._tmp.name) / "queue"
        for sub in ("pending", "claimed", "active", "review",
                    "parked", "failed", "done"):
            (self.queue / sub).mkdir(parents=True)
        self.messages = []

    def params(self):
        return InboundParams(queue_dir=self.queue, repo=REPO,
                             log=self.messages.append)

    def run_pass(self, api):
        return run_inbound(api, self.params())

    def pending(self, name):
        return self.queue / "pending" / name


class Ac2ImportTest(InboundTestCase):
    def test_ac2_import_then_idempotent_second_pass(self):
        api = FakeApi([_issue(7, "Test sync feature", body="# hello")])
        self.assertEqual(1, self.run_pass(api).imported)
        task = self.pending("Test_sync_feature.md")
        self.assertTrue(task.is_file())
        self.assertEqual("# hello", task.read_text())
        linkage = read_linkage(file_sidecar_path(task))
        self.assertIsNotNone(linkage)
        self.assertEqual(7, linkage.issue)
        self.assertEqual(REPO, linkage.repo)

        before = sorted(p.name for p in self.queue.rglob("*"))
        self.assertEqual(0, self.run_pass(api).imported)
        self.assertEqual(before, sorted(p.name for p in self.queue.rglob("*")))
        self.assertEqual("# hello", task.read_text())

    def test_only_open_snes_issues_are_listed(self):
        api = FakeApi([_issue(1, "closed one", state=IssueState.CLOSED),
                       _issue(2, "no trigger", labels=("bug",))])
        self.assertEqual(0, self.run_pass(api).imported)
        # The halt triggers also list (`snes-parked`/`snes-deleted`, open and
        # closed, Slice 4); the ingest query itself must stay open-only.
        ingest_calls = [state for labels, state in api.list_calls
                        if labels == ("snes",)]
        self.assertEqual([IssueState.OPEN], ingest_calls)
        self.assertEqual([], list((self.queue / "pending").iterdir()))


class BodyAndTitleFallbackTest(InboundTestCase):
    def test_empty_body_creates_title_url_stub(self):
        issue = _issue(9, "Blank body", body="   ")
        self.assertEqual(1, self.run_pass(FakeApi([issue])).imported)
        content = self.pending("Blank_body.md").read_text()
        self.assertIn("Blank body", content)
        self.assertIn(issue.html_url, content)

    def test_blank_title_falls_back_to_issue_number(self):
        self.assertEqual(1, self.run_pass(_issue_stub(11, "   .  ")).imported)
        self.assertTrue(self.pending("11.md").is_file())

    def test_title_cap_and_invalid_chars(self):
        long_title = "x" * (MAX_STEM_CHARS + 50)
        self.assertEqual(1, self.run_pass(_issue_stub(12, long_title)).imported)
        self.assertEqual(MAX_STEM_CHARS,
                         len(self.pending(f"{'x' * MAX_STEM_CHARS}.md").stem))
        self.assertEqual(1, self.run_pass(_issue_stub(13, "a/b\\c")).imported)
        self.assertTrue(self.pending("ab\\c.md").is_file())

    def test_leading_trailing_dots_stripped(self):
        self.assertEqual(1, self.run_pass(_issue_stub(14, "..dotted..")).imported)
        self.assertTrue(self.pending("dotted.md").is_file())


def _issue_stub(number, title):
    return FakeApi([_issue(number, title, body="body")])


class CollisionTest(InboundTestCase):
    def _linked_file(self, directory, name, issue_number):
        path = directory / name
        path.write_text("other task")
        write_linkage(file_sidecar_path(path),
                      SyncLinkage(issue=issue_number, repo=REPO))
        return path

    def test_collision_with_other_issues_file_gets_number_suffix(self):
        # A file titled like our issue but *linked* to issue 99: the
        # sidecar wins (FR-1.6), so our issue #7 must not claim it and the
        # derived filename gets the `-7` suffix (FR-1.1).
        self._linked_file(self.queue / "pending", "Same_name.md", 99)
        self.assertEqual(1, self.run_pass(_issue_stub(7, "Same name")).imported)
        imported = self.pending("Same_name-7.md")
        self.assertTrue(imported.is_file())
        self.assertEqual(7, read_linkage(file_sidecar_path(imported)).issue)
        self.assertEqual("other task",
                         (self.queue / "pending" / "Same_name.md").read_text())

    def test_overwrite_is_skipped_and_logged(self):
        self._linked_file(self.queue / "pending", "Dup.md", 99)
        blocked = self.pending("Dup-7.md")
        blocked.write_text("pre-existing")
        self.assertEqual(0, self.run_pass(_issue_stub(7, "Dup")).imported)
        self.assertEqual("pre-existing", blocked.read_text())
        self.assertTrue(any("skipping" in m for m in self.messages))

    def test_sidecar_match_in_active_dir_prevents_reimport(self):
        task_dir = self.queue / "active" / "some_task"
        task_dir.mkdir()
        write_linkage(task_dir / "gh.json",
                      SyncLinkage(issue=7, repo=REPO))
        self.assertEqual(0, self.run_pass(_issue_stub(7, "Whatever title")).imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))

    def test_unlinked_same_title_file_matches_by_title(self):
        (self.queue / "review" / "Fix_the_parser.md").write_text("summary")
        self.assertEqual(0, self.run_pass(_issue_stub(7, "Fix The Parser")).imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))


class DerivationUnitTest(InboundTestCase):
    def test_derive_filename_helpers(self):
        entries = scan_queue(self.queue)
        self.assertEqual("Test_sync_feature.md",
                         derive_task_filename(_issue(1, "Test sync feature"),
                                              entries))

    def test_find_matching_task_prefers_sidecar(self):
        task = self.pending("anything.md")
        task.write_text("body")
        write_linkage(file_sidecar_path(task),
                      SyncLinkage(issue=7, repo=REPO))
        entries = scan_queue(self.queue)
        match = find_matching_task(_issue(7, "Totally different"), entries)
        self.assertIsNotNone(match)
        self.assertEqual("anything", match.name)


class SyncPassTest(InboundTestCase):
    def _cfg(self):
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({
            "workDir": self._tmp.name, "githubPat": "ghp_token",
            "githubRepo": REPO}))
        return load(cfg_path)

    def test_sync_pass_reports_imports(self):
        api = FakeApi([_issue(7, "Test sync feature")])
        report = sync_pass(self._cfg(), api, log=self.messages.append)
        self.assertEqual(SyncReport(imported=1), report)
        self.assertIn("imported=1", report.summary_line())
        self.assertTrue(self.pending("Test_sync_feature.md").is_file())

    def test_sync_pass_reads_demo_enabled_from_config(self):
        # Slice 2 wiring: `demo.enabled` reaches the inbound parameters.
        cfg_path = Path(self._tmp.name) / "config-demo.json"
        cfg_path.write_text(json.dumps({
            "workDir": self._tmp.name, "githubPat": "ghp_token",
            "githubRepo": REPO, "demo": {"enabled": True}}))
        api = FakeApi([_issue(7, "Pizza fan site", labels=("snes-demo",))])
        report = sync_pass(load(cfg_path), api, log=self.messages.append)
        self.assertEqual(1, report.imported)
        self.assertTrue(read_linkage(file_sidecar_path(
            self.pending("Pizza_fan_site.md"))).demo)

    def test_sync_pass_without_demo_section_ignores_snes_demo(self):
        api = FakeApi([_issue(7, "Pizza fan site", labels=("snes-demo",))])
        report = sync_pass(self._cfg(), api, log=self.messages.append)
        self.assertEqual(0, report.imported)
        self.assertEqual([], list((self.queue / "pending").iterdir()))

    def test_cmd_sync_enabled_prints_summary(self):
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({
            "workDir": self._tmp.name, "githubPat": "ghp_token",
            "githubRepo": REPO}))
        api = FakeApi([_issue(7, "Test sync feature")])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            with mock.patch.object(handlers, "build_github_api",
                                   return_value=api):
                with contextlib.redirect_stdout(out):
                    rc = handlers.cmd_sync()
        self.assertEqual(0, rc)
        self.assertIn("github sync: imported=1", out.getvalue())
        self.assertTrue(self.pending("Test_sync_feature.md").is_file())

    def test_cmd_sync_reports_failure_without_crashing(self):
        cfg_path = Path(self._tmp.name) / "config.json"
        cfg_path.write_text(json.dumps({
            "workDir": self._tmp.name, "githubPat": "ghp_token",
            "githubRepo": REPO}))

        class _Boom:
            def list_issues(self, labels=(), state=None):
                raise RuntimeError("network down")

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            with mock.patch.object(handlers, "build_github_api",
                                   return_value=_Boom()):
                with contextlib.redirect_stdout(out):
                    rc = handlers.cmd_sync()
        self.assertEqual(1, rc)
        self.assertIn("github sync pass failed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
