"""Slice 12 — the hardening sweep (spec NFR-1, NFR-2, FR-0.2, AC-12, AC-13).

Everything here drives the *real* `GitHubApiClient` through an injected
fake transport, so the failure arrives at the HTTP edge exactly as it
would in production: a socket timeout, a 500, a 401, a spent rate limit.
Above the client sit the real sync modules, the real hook wrappers, the
real `harness sync` command and the real daemon loop.

Covered here:
  * AC-12 — each of the four failure modes, injected at the transport, is
    logged and swallowed at every hook site (stage-change hook, handoff
    hook), in `harness sync`, and in the daemon loop; nothing raises and
    the pipeline/daemon stays healthy;
  * a failure scoped to one issue stays a per-item skip: the pass finishes
    healthy and the other tasks still sync (NFR-1);
  * edge 3 — GitHub unreachable at a hook site: the task is untouched, the
    handoff prose still lands, and the next healthy pass reconciles the
    label that was missed;
  * AC-13 / FR-0.2 — a server that echoes the PAT back in its bodies and
    headers leaks nothing into logs, stats rows, task files, sidecars,
    comments or the daemon's output; the PAT appears only in the
    `Authorization` header of a request;
  * the §6 edge cases the earlier slices left unclaimed: edge 4 (two task
    filenames normalizing to one title — first wins, with a warning) and
    edge 8 (an active task with no sidecar whose only match is a closed
    issue is parked, never recreated);
  * NFR-3 read-through — HTTP modules are imported by `external/github_api.py`
    alone, no sync module reaches into `cli/`, and no sync module keeps a
    mutable module-level global.
All in-process: temp dirs, a fake transport, an injected clock (NFR-5).
"""
from __future__ import annotations

import ast
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import (  # noqa: E402
    GitHubApiClient, GitHubApiConfig, GitHubSyncDisabledError, HttpResponse,
)
from harness.cli import handlers  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.sync import SyncEngine, sync_pass  # noqa: E402
from harness.core.sync_comments import HandoffCommentPoster  # noqa: E402
from harness.core.sync_handoff_hook import HandoffSyncHook  # noqa: E402
from harness.core.sync_inbound import normalize_title  # noqa: E402
from harness.core.sync_outbound import run_outbound, OutboundParams  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    SyncLinkage, file_sidecar_path, write_legacy_linkage,
)
from harness.core.sync_stage_change_hook import run_stage_change_hook  # noqa: E402
from harness.core.syncd import (  # noqa: E402
    SYNC_FAILURE_THRESHOLD, SyncdLoop, SyncdParams,
)
from harness.workflow.task_lifecycle import TaskLifecycle  # noqa: E402

PAT = "ghp_Sup3rs3cr3tTokenValue0123456789"
REPO = "acme/widgets"
BASE = "https://api.github.test"
LOCATIONS = ("pending", "claimed", "active", "review",
             "parked", "failed", "done")


# ---------------------------------------------------------------------------
# The failure vocabulary (AC-12)
# ---------------------------------------------------------------------------

class FailureMode(Enum):
    """One injected HTTP failure, named after what the wire did."""
    NONE = "none"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"

    def is_abort_class(self) -> bool:
        """True when the failure ends a pass even mid-item.

        A spent rate-limit budget and an auth disable propagate through the
        per-item handlers and end the pass (spec edge 9, FR-5). A timeout
        and a 500 stay per-item wherever a per-item handler covers the
        call; injected globally they also end the pass, because the
        phase's own listing fails and there is nothing left to iterate
        (see `test_failure_scoped_to_one_issue_stays_a_per_item_skip`).
        """
        return self in (FailureMode.RATE_LIMIT, FailureMode.AUTH)


@dataclass
class FailureRule:
    """Apply `mode` to every request whose path contains `path_contains`
    (empty string: to every request)."""
    mode: FailureMode
    path_contains: str = ""


def _echo(headers: Mapping[str, str]) -> str:
    """An error body that repeats the credential back, verbatim.

    The nastiest leak shape FR-0.2 has to survive: the server quotes the
    whole `Authorization` header, so any message built from the body
    carries the PAT unless it was scrubbed.
    """
    return json.dumps({"message": "You supplied "
                                  f"{headers.get('Authorization', '')}",
                       "documentation_url": f"{BASE}/docs"})


def _failure_response(mode: FailureMode,
                      headers: Mapping[str, str]) -> HttpResponse:
    """The response (or raised exception) `mode` produces."""
    if mode is FailureMode.TIMEOUT:
        raise socket.timeout("The read operation timed out")
    if mode is FailureMode.SERVER_ERROR:
        return HttpResponse(500, {"Retry-After": "0"}, _echo(headers))
    if mode is FailureMode.AUTH:
        return HttpResponse(401, {}, _echo(headers))
    return HttpResponse(403, {"Retry-After": "0",
                              "X-RateLimit-Remaining": "0"}, _echo(headers))


# ---------------------------------------------------------------------------
# A tiny GitHub at the HTTP edge
# ---------------------------------------------------------------------------

def _issue_json(number: int, title: str, labels=("snes",),
                state="open", body="issue body") -> dict:
    return {"number": number, "title": title, "body": body, "state": state,
            "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.test/{REPO}/issues/{number}"}


@dataclass
class RecordedRequest:
    method: str
    url: str
    path: str
    headers: dict
    body: str | None


class FakeGithubTransport:
    """The transport `GitHubApiClient` talks to: an in-memory GitHub.

    Serves the routes the sync uses and records every request, so a test
    can assert both the mutations that reached the API and the fact that
    the PAT travelled in exactly one header. `rules` inject failures
    before routing (AC-12).
    """

    def __init__(self, issues=(), rules=()):
        self.issues = {issue["number"]: dict(issue) for issue in issues}
        self.comments: dict[int, list[dict]] = {}
        self.rules = list(rules)
        self.requests: list[RecordedRequest] = []
        self._next_comment = 500

    def __call__(self, method: str, url: str, headers: Mapping[str, str],
                 body: str | None) -> HttpResponse:
        split = urllib.parse.urlsplit(url)
        self.requests.append(RecordedRequest(method, url, split.path,
                                             dict(headers), body))
        for rule in self.rules:
            if rule.mode is not FailureMode.NONE and (
                    not rule.path_contains or rule.path_contains in split.path):
                return _failure_response(rule.mode, headers)
        return self._route(method, split.path,
                           urllib.parse.parse_qs(split.query),
                           json.loads(body) if body else None)

    # -- route table -------------------------------------------------------

    def _route(self, method, path, query, body):
        root = f"/repos/{REPO}/issues"
        if path == root:
            return self._issues(method, query, body)
        remainder = path[len(root) + 1:] if path.startswith(root + "/") else ""
        parts = remainder.split("/")
        number = int(parts[0])
        if len(parts) == 1:
            return self._issue_one(method, number, body)
        if parts[1] == "labels":
            return self._labels(method, number, parts, body)
        if parts[1] == "comments":
            return self._comments(method, number, body)
        return HttpResponse(404, {}, '{"message": "no route"}')

    def _issues(self, method, query, body):
        if method == "POST":
            number = max(self.issues, default=0) + 1
            self.issues[number] = _issue_json(
                number, body["title"], labels=(), body=body["body"])
            return HttpResponse(201, {},
                                json.dumps(self.issues[number]))
        wanted = {name for name in
                  (query.get("labels") or [""])[0].split(",") if name}
        state = (query.get("state") or ["open"])[0]
        page = [issue for issue in sorted(self.issues.values(),
                                          key=lambda i: i["number"])
                if issue["state"] == state
                and wanted <= {label["name"] for label in issue["labels"]}]
        return HttpResponse(200, {}, json.dumps(page))

    def _issue_one(self, method, number, body):
        issue = self._issue_or_404(number)
        if method == "PATCH":
            issue["state"] = body.get("state", issue["state"])
        return HttpResponse(200, {}, json.dumps(issue))

    def _labels(self, method, number, parts, body):
        issue = self._issue_or_404(number)
        names = [label["name"] for label in issue["labels"]]
        if method == "POST":
            issue["labels"] = [{"name": name}
                               for name in names + [n for n in body["labels"]
                                                    if n not in names]]
            return HttpResponse(201, {}, json.dumps(issue["labels"]))
        if method == "DELETE":
            gone = urllib.parse.unquote(parts[2])
            issue["labels"] = [label for label in issue["labels"]
                               if label["name"] != gone]
            return HttpResponse(204, {}, json.dumps(issue["labels"]))
        return HttpResponse(200, {}, json.dumps(issue["labels"]))

    def _comments(self, method, number, body):
        self._issue_or_404(number)
        posted = self.comments.setdefault(number, [])
        if method == "POST":
            comment = {"id": self._next_comment, "body": body["body"],
                       "html_url": f"https://github.test/{REPO}"
                                   f"/issues/{number}#issuecomment"}
            self._next_comment += 1
            posted.append(comment)
            return HttpResponse(201, {}, json.dumps(comment))
        return HttpResponse(200, {}, json.dumps(posted))

    def _issue_or_404(self, number):
        if number not in self.issues:
            raise AssertionError(f"fake GitHub has no issue #{number}")
        return self.issues[number]

    # -- assertions helpers --------------------------------------------------

    def carried(self, number) -> set[str]:
        issue = self.issues.get(number) or {}
        return {label["name"] for label in issue.get("labels", [])}

    def posted_comments(self) -> list[str]:
        return [comment["body"]
                for bodies in self.comments.values() for comment in bodies]


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

class FailureTestCase(unittest.TestCase):
    """Temp queue + real config + real client over the fake transport."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue = self.work_dir / "queue"
        for sub in LOCATIONS:
            (self.queue / sub).mkdir(parents=True)
        cfg_path = self.work_dir / "config.json"
        cfg_path.write_text(json.dumps({
            "harnessExecutionAndQueueDir": str(self.work_dir), "githubPat": PAT,
            "githubRepo": REPO, "githubApiBaseUrl": BASE}))
        self.cfg = load(cfg_path)
        self.messages: list[str] = []

    def transport(self, issues=(), rules=()) -> FakeGithubTransport:
        self.fake = FakeGithubTransport(issues=issues, rules=rules)
        return self.fake

    def client(self, transport) -> GitHubApiClient:
        """A real client: fake transport, fake clock, log captured.

        The injected `sleep` keeps the retry budget free (the client would
        otherwise wait out `Retry-After` for real), so a pass exercises
        all three attempts in milliseconds.
        """
        return GitHubApiClient(GitHubApiConfig(pat=PAT, repo=REPO,
                                               base_url=BASE),
                               transport=transport,
                               log=self.messages.append,
                               sleep=lambda seconds: None)

    def engine(self, issues=(), rules=()) -> SyncEngine:
        api = self.client(self.transport(issues=issues, rules=rules))
        poster = HandoffCommentPoster(api, self.queue, REPO,
                                      log=self.messages.append)
        return SyncEngine(self.cfg, api, log=self.messages.append,
                          comment_poster=poster)

    def file_task(self, name, location="pending", issue=None,
                  body=None) -> Path:
        path = self.queue / location / f"{name}.md"
        path.write_text(body if body is not None else f"# {name} body")
        if issue is not None:
            write_legacy_linkage(file_sidecar_path(path),
                          SyncLinkage(issue=issue, repo=REPO))
        return path

    def dir_task(self, name, location="active", issue=None) -> Path:
        task_dir = self.queue / location / name
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(
            json.dumps({"id": name, "status": location}))
        (task_dir / "original.md").write_text(f"# {name} body")
        if issue is not None:
            write_legacy_linkage(task_dir / "gh.json",
                          SyncLinkage(issue=issue, repo=REPO))
        return task_dir

    def assert_no_pat(self, text: str, where: str) -> None:
        self.assertNotIn(PAT, text, f"the PAT leaked into {where}")

    def assert_healthy(self, report, mode: FailureMode) -> None:
        """A pass never raises, always reports, and never leaks the PAT.

        Every mode is injected on *every* request here, so the phase's own
        `list_issues` fails and the pass cannot complete: it reports the
        abort rather than raising, whichever class the failure is in. The
        abort-class distinction (edge 9 vs NFR-1) is about what happens
        when a per-item handler covers the call, tested separately.
        """
        self.assert_no_pat(report.summary_line(), "the summary line")
        self.assert_no_pat(report.abort_reason, "the abort reason")
        self.assertTrue(report.aborted)
        self.assertTrue(any("pass aborted" in m for m in self.messages))
        if mode.is_abort_class():
            self.assertIn("ABORTED", report.summary_line())


# ---------------------------------------------------------------------------
# AC-12 — the four failures through the pass engine
# ---------------------------------------------------------------------------

class PassFailureTest(FailureTestCase):
    """`sync_pass()` survives every injected HTTP failure (AC-12, NFR-1)."""

    def _run(self, mode: FailureMode):
        issues = [_issue_json(1, "Linked task", labels=("snes", "snes-pending")),
                  _issue_json(2, "Other task", labels=("snes", "snes-pending"))]
        self.file_task("linked_task", issue=1)
        self.file_task("other_task", issue=2)
        engine = self.engine(issues=issues, rules=[FailureRule(mode)])
        return sync_pass(self.cfg, engine.api, log=self.messages.append)

    def test_every_mode_is_reported_never_raised(self):
        for mode in FailureMode:
            if mode is FailureMode.NONE:
                continue
            with self.subTest(mode=mode.value):
                self.setUp()  # fresh queue and fresh log per mode
                report = self._run(mode)
                self.assert_healthy(report, mode)
                self.assertTrue(any("github sync" in m
                                    for m in self.messages))

    def test_failure_scoped_to_one_issue_stays_a_per_item_skip(self):
        """NFR-1's real shape: one issue's labels endpoint 500s, the pass
        finishes healthy, the other task is still labeled."""
        issues = [_issue_json(1, "Linked task", labels=("snes",)),
                  _issue_json(2, "Other task", labels=("snes",))]
        self.file_task("linked_task", issue=1)
        self.file_task("other_task", issue=2)
        engine = self.engine(
            issues=issues,
            rules=[FailureRule(FailureMode.SERVER_ERROR, "/issues/1/labels")])
        report = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assertFalse(report.aborted)
        self.assertEqual(1, report.label_updates)
        self.assertEqual({"snes", "snes-pending"}, self.fake.carried(2))
        self.assertTrue(any("outbound sync failed" in m
                            for m in self.messages))

    def test_rate_limit_aborts_the_pass_and_rolls_to_the_next_one(self):
        """Edge 9 end-to-end: the first pass aborts, the next (healthy)
        pass does the work it never reached."""
        issues = [_issue_json(1, "Linked task", labels=("snes",))]
        self.file_task("linked_task", issue=1)
        engine = self.engine(issues=issues,
                             rules=[FailureRule(FailureMode.RATE_LIMIT)])
        aborted = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assertTrue(aborted.aborted)
        self.assertEqual({"snes"}, self.fake.carried(1))
        engine.api.reset_pass()
        self.fake.rules = []
        recovered = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assertFalse(recovered.aborted)
        self.assertEqual({"snes", "snes-pending"}, self.fake.carried(1))

    def test_auth_failure_disables_the_pass_and_logs_one_error(self):
        """FR-5 through the engine: one auth error line, no further HTTP,
        and the next pass is armed again."""
        issues = [_issue_json(1, "Linked task", labels=("snes",))]
        self.file_task("linked_task", issue=1)
        engine = self.engine(issues=issues,
                             rules=[FailureRule(FailureMode.AUTH)])
        report = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assertTrue(report.aborted)
        auth_lines = [m for m in self.messages if "GITHUB-SYNC-AUTH-ERROR" in m]
        self.assertEqual(1, len(auth_lines))
        self.assert_no_pat("\n".join(self.messages), "the auth error log")
        self.assertTrue(engine.api.pass_disabled)
        requests_after_disable = len(self.fake.requests)
        with self.assertRaises(GitHubSyncDisabledError):
            engine.api.list_issues()
        self.assertEqual(requests_after_disable, len(self.fake.requests),
                         "a disabled pass must not touch the transport")
        engine.api.reset_pass()
        self.assertFalse(engine.api.pass_disabled)


# ---------------------------------------------------------------------------
# AC-12 — the same four failures at every hook site
# ---------------------------------------------------------------------------

class HookSiteFailureTest(FailureTestCase):
    """A hook site never fails a task, a handoff or the CLI (NFR-1)."""

    def _linked_active_task(self) -> Path:
        return self.dir_task("mover", location="active", issue=1)

    def test_stage_change_hook_survives_every_mode(self):
        for mode in FailureMode:
            if mode is FailureMode.NONE:
                continue
            with self.subTest(mode=mode.value):
                self.setUp()
                self._linked_active_task()
                engine = self.engine(
                    issues=[_issue_json(1, "mover",
                                        labels=("snes", "snes-active"))],
                    rules=[FailureRule(mode)])
                run_stage_change_hook(engine, "mover",
                                      log=self.messages.append)
                self.assert_no_pat("\n".join(self.messages),
                                   "the stage-change hook log")

    def test_handoff_hook_survives_every_mode(self):
        for mode in FailureMode:
            if mode is FailureMode.NONE:
                continue
            with self.subTest(mode=mode.value):
                self.setUp()
                task_dir = self._linked_active_task()
                engine = self.engine(
                    issues=[_issue_json(1, "mover",
                                        labels=("snes", "snes-active"))],
                    rules=[FailureRule(mode)])
                hook = HandoffSyncHook(engine, engine.comment_poster,
                                       log=self.messages.append)
                hook("mover", "implement", "continuation prose")
                self.assert_no_pat("\n".join(self.messages),
                                   "the handoff hook log")
                # NFR-1: the handoff's own artifact is untouched by sync.
                self.assertTrue((task_dir / "task.json").is_file())

    def test_cmd_sync_exits_zero_in_every_mode(self):
        for mode in FailureMode:
            if mode is FailureMode.NONE:
                continue
            with self.subTest(mode=mode.value):
                self.setUp()
                self.file_task("linked_task", issue=1)
                engine = self.engine(
                    issues=[_issue_json(1, "Linked task",
                                        labels=("snes", "snes-pending"))],
                    rules=[FailureRule(mode)])
                out = io.StringIO()
                with mock.patch.dict(
                        os.environ,
                        {"HARNESS_CONFIG": str(self.work_dir / "config.json")}), \
                        mock.patch.object(handlers, "build_github_api",
                                          lambda cfg, log=None: engine.api):
                    with redirect_stdout(out):
                        rc = handlers.cmd_sync()
                self.assertEqual(0, rc)
                self.assertIn("github sync:", out.getvalue())
                self.assert_no_pat(out.getvalue(), "the harness sync output")

    def test_daemon_loop_survives_every_mode_and_still_watches_work(self):
        """AC-12 in the daemon: the production sync dispatch fails every
        pass, the loop keeps running, backs off once, and still spawns."""
        for mode in FailureMode:
            if mode is FailureMode.NONE:
                continue
            with self.subTest(mode=mode.value):
                self.setUp()
                engine = self.engine(
                    issues=[_issue_json(1, "Linked task",
                                        labels=("snes", "snes-pending"))],
                    rules=[FailureRule(mode)])
                slept: list[float] = []
                spawns: list[int] = []

                def spawn() -> int:
                    # A real child: the loop reaps dead children, so a
                    # fake pid would read as dead and re-spawn each pass.
                    child = subprocess.Popen(
                        [sys.executable, "-c",
                         "import time; time.sleep(1)"])
                    self.addCleanup(child.wait)
                    spawns.append(child.pid)
                    return child.pid

                loop = SyncdLoop(SyncdParams(
                    work_dir=self.work_dir, sync_interval_s=10.0,
                    sync=lambda: engine.on_stage_change(),
                    spawn=spawn,
                    log=self.messages.append,
                    sleep=lambda seconds, stop: slept.append(seconds),
                    check_pending=lambda: True,
                    stop_after_passes=SYNC_FAILURE_THRESHOLD + 1))
                self.assertEqual(0, loop.run())
                self.assertEqual(1, len(spawns))
                joined = "\n".join(self.messages)
                self.assert_no_pat(joined, "the daemon log")
                self.assertIn("backing off", joined)
                self.assertIn(10.0 * 5, slept)
        # Note: the former fake child pid (os.getpid()) was replaced by a
        # real child — the daemon now reaps dead children, so a non-child
        # pid reads as dead and would re-spawn on every pass.


# ---------------------------------------------------------------------------
# Edge 3 — unreachable at a hook site, reconciled by the next pass
# ---------------------------------------------------------------------------

class ReconciliationTest(FailureTestCase):
    def test_unreachable_hook_site_leaves_the_task_and_reconciles_later(self):
        task_dir = self.dir_task("mover", location="active", issue=7)
        issues = [_issue_json(7, "mover", labels=("snes",))]
        engine = self.engine(issues=issues,
                             rules=[FailureRule(FailureMode.TIMEOUT)])
        run_stage_change_hook(engine, "mover", log=self.messages.append)
        # The task is exactly where it was; nothing was written anywhere.
        self.assertTrue((task_dir / "task.json").is_file())
        self.assertEqual({"snes"}, self.fake.carried(7))
        self.assertTrue(any("github sync" in m for m in self.messages))
        # The network comes back: the next pass applies the missed label.
        self.fake.rules = []
        report = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assertFalse(report.aborted)
        self.assertEqual({"snes", "snes-active"}, self.fake.carried(7))


# ---------------------------------------------------------------------------
# AC-13 / FR-0.2 — no PAT material anywhere the sync writes
# ---------------------------------------------------------------------------

class SecretScrubbingTest(FailureTestCase):
    """A server that echoes the credential back leaves no trace on disk."""

    def _sweep(self):
        """Drive every writer the sync owns, under auth + 500 failures."""
        issues = [_issue_json(1, "Mover", labels=("snes", "snes-active")),
                  _issue_json(2, "New task", labels=())]
        task_dir = self.dir_task("mover", location="active", issue=1)
        self.file_task("new_task", body="# new task\n")
        engine = self.engine(issues=issues,
                             rules=[FailureRule(FailureMode.AUTH)])
        poster = engine.comment_poster
        hook = HandoffSyncHook(engine, poster, log=self.messages.append)
        hook("mover", "implement", "continuation prose")
        run_stage_change_hook(engine, "mover", log=self.messages.append)
        sync_pass(self.cfg, engine.api, log=self.messages.append)
        # A healthy pass too, so the writers that succeed are swept as well.
        self.fake.rules = []
        engine.api.reset_pass()
        sync_pass(self.cfg, engine.api, log=self.messages.append)
        poster("mover", "review", "review prose")
        return task_dir

    def test_pat_absent_from_logs_files_sidecars_and_comments(self):
        task_dir = self._sweep()
        self.assert_no_pat("\n".join(self.messages), "the harness log sink")
        written = [path for path in self.work_dir.rglob("*")
                   if path.is_file() and path.name != "config.json"]
        self.assertTrue(written, "the sweep wrote nothing to sweep")
        for path in written:
            self.assert_no_pat(
                path.read_text(errors="replace"),
                f"{path.relative_to(self.work_dir)}")
        for comment in self.fake.posted_comments():
            self.assert_no_pat(comment, "a posted issue comment")
        self.assertTrue((task_dir / "task.json").is_file())

    def test_pat_travels_only_in_the_authorization_header(self):
        self._sweep()
        self.assertTrue(self.fake.requests)
        for request in self.fake.requests:
            self.assert_no_pat(request.url, "a request URL")
            self.assert_no_pat(request.body or "", "a request body")
            for name, value in request.headers.items():
                if name.lower() == "authorization":
                    self.assertIn(PAT, value)
                else:
                    self.assert_no_pat(value, f"the {name} header")

    def test_error_bodies_that_echo_the_pat_are_scrubbed(self):
        """The 500/401 bodies quote the whole Authorization header; the
        messages the client raises must not carry it through."""
        self.file_task("linked_task", issue=1)
        engine = self.engine(issues=[_issue_json(1, "Linked task")],
                             rules=[FailureRule(FailureMode.SERVER_ERROR)])
        report = sync_pass(self.cfg, engine.api, log=self.messages.append)
        self.assert_no_pat(report.abort_reason, "the abort reason")
        self.assert_no_pat("\n".join(self.messages), "the pass log")


# ---------------------------------------------------------------------------
# §6 edge cases the earlier slices left unclaimed
# ---------------------------------------------------------------------------

class EdgeCaseClosureTest(FailureTestCase):
    def test_edge4_two_filenames_one_title_first_wins_with_warning(self):
        """Edge 4 (FR-2.1): `fix_the_parser` and `Fix_the_parser` normalize
        to one title. One task owns the issue, the other is skipped loudly
        rather than fighting for the same state label every pass."""
        self.file_task("fix_the_parser")
        self.file_task("Fix_the_parser")
        self.assertEqual(normalize_title("Fix_the_parser"),
                         normalize_title("fix_the_parser"))
        engine = self.engine()
        result = run_outbound(engine.api, OutboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append,
            lifecycle=TaskLifecycle(self.cfg, log=self.messages.append)))
        self.assertEqual(1, result.created_issues)
        self.assertEqual(1, len([issue for issue in self.fake.issues.values()
                                 if normalize_title(issue["title"])
                                 == normalize_title("fix_the_parser")]))
        self.assertTrue(any("first" in m and "title" in m
                            for m in self.messages),
                        f"no first-wins warning in {self.messages}")

    def test_edge8_active_task_without_sidecar_closed_match_is_parked(self):
        """Edge 8: an active dir task with no sidecar, no open match and a
        closed match parks — it never recreates the issue."""
        task_dir = self.dir_task("orphan", location="active")
        engine = self.engine(
            issues=[_issue_json(9, "orphan", state="closed")])
        result = run_outbound(engine.api, OutboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append,
            lifecycle=TaskLifecycle(self.cfg, log=self.messages.append)))
        self.assertEqual(1, result.parked)
        self.assertEqual(0, result.created_issues)
        self.assertFalse(task_dir.exists())
        self.assertTrue((self.queue / "parked" / "orphan").is_dir())
        self.assertEqual(0, sum(1 for number in self.fake.issues
                                if number != 9))
        # Idempotent: the parked task is not parked again, not recreated.
        again = run_outbound(engine.api, OutboundParams(
            queue_dir=self.queue, repo=REPO, log=self.messages.append,
            lifecycle=TaskLifecycle(self.cfg, log=self.messages.append)))
        self.assertEqual(0, again.parked + again.created_issues)


# ---------------------------------------------------------------------------
# NFR-3 read-through, automated
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

SYNC_FEATURE_FILES = (
    [REPO_ROOT / "external" / "github_api.py"]
    + sorted((REPO_ROOT / "harness" / "core").glob("sync*.py"))
    + [REPO_ROOT / "harness" / "composition.py",
       REPO_ROOT / "harness" / "cli" / "handlers.py",
       REPO_ROOT / "harness" / "cli" / "parser.py"]
    + sorted((REPO_ROOT / "harness" / "workflow").glob("*.py"))
)

HTTP_MODULES = {"urllib", "http", "socket", "requests", "aiohttp",
                "httplib2", "httpx"}
# A module-level type alias, not state; every other global is a constant.
ALLOWED_NON_CONSTANT_GLOBALS = frozenset({"Transport"})


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _module_level_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets
                         if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                            ast.Name):
            names.append(node.target.id)
    return names


class StandardsReadThroughTest(unittest.TestCase):
    """The slice-12 read-through, expressed as tests instead of memory."""

    def test_http_modules_live_only_in_the_api_boundary(self):
        for path in SYNC_FEATURE_FILES:
            if path.name == "github_api.py":
                continue
            with self.subTest(file=path.name):
                for module in _imported_modules(path):
                    root = module.split(".")[0]
                    self.assertNotIn(
                        root, HTTP_MODULES,
                        f"{path.relative_to(REPO_ROOT)} imports {module}; "
                        "HTTP belongs in external/github_api.py alone")

    def test_sync_modules_never_reach_up_into_the_cli(self):
        for path in sorted((REPO_ROOT / "harness" / "core").glob("sync*.py")) \
                + [REPO_ROOT / "harness" / "composition.py"]:
            with self.subTest(file=path.name):
                for module in _imported_modules(path):
                    self.assertNotIn("cli", module.split("."),
                                     f"{path.name} imports {module}")

    def test_sync_modules_hold_no_mutable_global_state(self):
        """CODING_STANDARDS §5: config is passed down, never parked in a
        module-level name a function can rewrite."""
        for path in SYNC_FEATURE_FILES:
            with self.subTest(file=path.name):
                for name in _module_level_names(path):
                    self.assertTrue(
                        name.isupper() or name in ALLOWED_NON_CONSTANT_GLOBALS,
                        f"{path.relative_to(REPO_ROOT)} defines the global "
                        f"{name!r}; constants are UPPER_SNAKE and state is "
                        "passed explicitly")

    def test_disabled_config_still_observably_changes_nothing(self):
        """NFR-2 re-checked after the whole feature exists: an unconfigured
        `harness sync` prints the disabled line and builds no client."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            cfg_path = work / "config.json"
            cfg_path.write_text(json.dumps({"harnessExecutionAndQueueDir": str(work)}))
            builder = mock.Mock(side_effect=AssertionError("HTTP client built"))
            out = io.StringIO()
            with mock.patch.dict(os.environ,
                                 {"HARNESS_CONFIG": str(cfg_path)}), \
                    mock.patch.object(handlers, "build_github_api", builder):
                with redirect_stdout(out):
                    rc = handlers.cmd_sync()
            self.assertEqual(0, rc)
            self.assertIn("github sync disabled", out.getvalue())
            builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
