"""Slice 2 — the GitHub REST client `external/github_api.py` (spec FR-5).

Every test drives `GitHubApiClient` through an injected fake transport:
no network, no real repo, no clock waits (NFR-5). The fake records every
request (method, url, headers, body) and replays canned `HttpResponse`
fixtures.

Covered here:
  * every op returns typed dataclasses parsed from canned JSON, and every
    request carries the four fixed FR-5 headers;
  * pagination walks the `Link` rel="next" header and falls back to the
    `page` query param on a full page;
  * retries ≤ 3 on 5xx and rate-limit responses, honoring `Retry-After`
    and `X-RateLimit-Reset` with bounded backoff; a spent budget raises
    the typed error (`GitHubServerError` / `GitHubRateLimitError`);
  * 401 and non-rate-limit 403 disable the sync for the remainder of the
    pass: one clear auth error logged, later calls fail fast without
    touching the transport, `reset_pass()` re-arms (FR-5);
  * label mutations are add/remove only — no replace-all op exists
    (FR-2.4);
  * the PAT never appears in a raised error string or a log line, even
    when the server echoes it back (FR-0.2);
  * transport failures (timeout) wrap into `GitHubTransportError`
    without retry (spec edge 3).
"""
from __future__ import annotations

import json
import socket
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import (  # noqa: E402
    ACCEPT,
    API_VERSION,
    GitHubApiClient,
    GitHubApiConfig,
    GitHubApiError,
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubServerError,
    GitHubSyncDisabledError,
    GitHubTransportError,
    HttpResponse,
    Issue,
    IssueState,
    Label,
    PER_PAGE,
    REDACTED,
    USER_AGENT,
)

from harness.core.sync_labels import StateLabel, TriggerLabel  # noqa: E402

PAT = "ghp_supersecrettokenvalue1234567890"
REPO = "acme/widgets"
BASE = "https://api.github.com"


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict
    body: str | None


class FakeTransport:
    """Replays queued responses and records requests. A queued exception
    is raised instead of returned (simulates a dead transport)."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[RecordedRequest] = []

    def __call__(self, method: str, url: str,
                 headers: Mapping[str, str], body: str | None) -> HttpResponse:
        self.requests.append(RecordedRequest(method, url, dict(headers), body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def resp(status: int = 200, body: str = "",
         headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status=status, headers=dict(headers or {}), body=body)


def json_resp(payload, status: int = 200,
              headers: Mapping[str, str] | None = None) -> HttpResponse:
    return resp(status, json.dumps(payload), headers)


ISSUE_JSON = {
    "number": 7,
    "title": "Test sync feature",
    "body": "# hello",
    "state": "open",
    "html_url": "https://github.com/acme/widgets/issues/7",
    "labels": [{"name": "snes", "color": "ededed"},
               {"name": "bug", "color": "d73a4a"}],
}

COMMENT_JSON = {"id": 55, "body": "handoff prose",
                "html_url": "https://github.com/acme/widgets/issues/7#issuecomment-55"}


class ClientFixture(unittest.TestCase):
    """Shared scaffolding: fake transport, recorded sleeps, captured log."""

    def make_client(self, responses: list) -> tuple[GitHubApiClient,
                                                    FakeTransport]:
        self.sleeps: list[float] = []
        self.logs: list[str] = []
        self.transport = FakeTransport(responses)
        client = GitHubApiClient(GitHubApiConfig(pat=PAT, repo=REPO,
                                                 base_url=BASE),
                                 transport=self.transport,
                                 log=self.logs.append,
                                 sleep=self.sleeps.append)
        return client, self.transport


class TestIssueOps(ClientFixture):
    def test_get_issue_returns_typed_dataclass(self):
        client, transport = self.make_client([json_resp(ISSUE_JSON)])
        issue = client.get_issue(7)
        self.assertEqual(issue, Issue(
            number=7, title="Test sync feature", body="# hello",
            state=IssueState.OPEN,
            labels=(Label("snes"), Label("bug")),
            html_url="https://github.com/acme/widgets/issues/7"))
        call = transport.requests[0]
        self.assertEqual(call.method, "GET")
        self.assertEqual(call.url, f"{BASE}/repos/acme/widgets/issues/7")

    def test_every_request_carries_fixed_headers(self):
        client, transport = self.make_client([json_resp(ISSUE_JSON)])
        client.get_issue(7)
        headers = transport.requests[0].headers
        self.assertEqual(headers["Authorization"], f"Bearer {PAT}")
        self.assertEqual(headers["Accept"], ACCEPT)
        self.assertEqual(headers["X-GitHub-Api-Version"], API_VERSION)
        self.assertEqual(headers["User-Agent"], USER_AGENT)

    def test_list_issues_filters_by_label_and_state(self):
        client, transport = self.make_client([json_resp([ISSUE_JSON])])
        issues = client.list_issues(labels=[TriggerLabel.INGEST.value],
                                    state=IssueState.OPEN)
        self.assertEqual([i.number for i in issues], [7])
        url = transport.requests[0].url
        self.assertIn("state=open", url)
        self.assertIn("labels=snes", url)
        self.assertIn("per_page=100", url)

    def test_list_issues_closed_state(self):
        closed = dict(ISSUE_JSON, state="closed")
        client, transport = self.make_client([json_resp([closed])])
        issues = client.list_issues(state=IssueState.CLOSED)
        self.assertEqual(issues[0].state, IssueState.CLOSED)
        self.assertIn("state=closed", transport.requests[0].url)

    def test_create_issue_posts_title_and_body(self):
        created = dict(ISSUE_JSON, number=9)
        client, transport = self.make_client([json_resp(created)])
        issue = client.create_issue("fix the parser", "the body")
        self.assertEqual(issue.number, 9)
        call = transport.requests[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.url, f"{BASE}/repos/acme/widgets/issues")
        self.assertEqual(json.loads(call.body),
                         {"title": "fix the parser", "body": "the body"})

    def test_close_issue_patches_state_closed(self):
        closed = dict(ISSUE_JSON, state="closed")
        client, transport = self.make_client([json_resp(closed)])
        issue = client.close_issue(7)
        self.assertEqual(issue.state, IssueState.CLOSED)
        call = transport.requests[0]
        self.assertEqual(call.method, "PATCH")
        self.assertEqual(json.loads(call.body), {"state": "closed"})


class TestPagination(ClientFixture):
    def test_link_header_next_is_walked(self):
        page1 = [{"number": n, "title": f"t{n}", "body": "", "state": "open",
                  "html_url": "", "labels": []} for n in range(1, 3)]
        page2 = [{"number": 3, "title": "t3", "body": "", "state": "open",
                  "html_url": "", "labels": []}]
        link = f'<{BASE}/repos/acme/widgets/issues?page=2>; rel="next", ' \
               f'<{BASE}/repos/acme/widgets/issues?page=1>; rel="prev"'
        client, transport = self.make_client(
            [json_resp(page1, headers={"Link": link}), json_resp(page2)])
        issues = client.list_issues()
        self.assertEqual([i.number for i in issues], [1, 2, 3])
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(transport.requests[1].url,
                         f"{BASE}/repos/acme/widgets/issues?page=2")

    def test_full_page_without_link_pages_by_param(self):
        def page(numbers):
            return [{"number": n, "title": "t", "body": "", "state": "open",
                     "html_url": "", "labels": []} for n in numbers]
        client, transport = self.make_client(
            [json_resp(page(range(PER_PAGE))), json_resp([])])
        issues = client.list_issues()
        self.assertEqual(len(issues), PER_PAGE)
        self.assertEqual(len(transport.requests), 2)
        self.assertIn("page=2", transport.requests[1].url)

    def test_list_comments_paginates(self):
        client, _ = self.make_client(
            [json_resp([COMMENT_JSON]), json_resp([])])
        # First page has 1 item (< PER_PAGE) so a single request suffices.
        comments = client.list_comments(7)
        self.assertEqual(comments[0].id, 55)
        self.assertEqual(comments[0].body, "handoff prose")


class TestLabelOps(ClientFixture):
    def test_list_labels(self):
        client, transport = self.make_client(
            [json_resp([{"name": "snes"}, {"name": "human-label"}])])
        labels = client.list_labels(7)
        self.assertEqual([l.name for l in labels], ["snes", "human-label"])
        self.assertEqual(transport.requests[0].method, "GET")
        self.assertTrue(transport.requests[0].url.endswith(
            "/repos/acme/widgets/issues/7/labels"))

    def test_add_labels_posts_diff_only(self):
        client, transport = self.make_client(
            [json_resp([{"name": StateLabel.ACTIVE.value}])])
        labels = client.add_labels(7, [StateLabel.ACTIVE.value])
        self.assertEqual(labels, [Label("snes-active")])
        call = transport.requests[0]
        self.assertEqual(call.method, "POST")
        self.assertTrue(call.url.endswith("/repos/acme/widgets/issues/7/labels"))
        self.assertEqual(json.loads(call.body), {"labels": ["snes-active"]})

    def test_remove_label_deletes_by_quoted_name(self):
        client, transport = self.make_client([resp(204)])
        client.remove_label(7, "snes-pending")
        call = transport.requests[0]
        self.assertEqual(call.method, "DELETE")
        self.assertEqual(call.url,
                         f"{BASE}/repos/acme/widgets/issues/7/labels/snes-pending")

    def test_no_replace_all_op_exists(self):
        # FR-2.4: label mutation must never use PUT-replace.
        for name in dir(GitHubApiClient):
            self.assertFalse(name.startswith("set_labels"),
                             "replace-all label op must not exist")
        import inspect
        source = inspect.getsource(sys.modules["external.github_api"])
        self.assertNotIn('"PUT"', source)


class TestRetry(ClientFixture):
    def test_5xx_then_success_retries_with_backoff(self):
        client, transport = self.make_client(
            [resp(502, "bad gateway"), json_resp(ISSUE_JSON)])
        issue = client.get_issue(7)
        self.assertEqual(issue.number, 7)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.sleeps, [1.0])

    def test_retry_honors_retry_after_on_5xx(self):
        client, _ = self.make_client(
            [resp(503, "", {"Retry-After": "4"}), json_resp(ISSUE_JSON)])
        client.get_issue(7)
        self.assertEqual(self.sleeps, [4.0])

    def test_retry_delay_is_bounded(self):
        client, _ = self.make_client(
            [resp(503, "", {"Retry-After": "3600"}), json_resp(ISSUE_JSON)])
        client.get_issue(7)
        self.assertEqual(self.sleeps, [60.0])

    def test_secondary_rate_limit_403_retries(self):
        client, transport = self.make_client(
            [resp(403, "secondary rate limit", {"Retry-After": "2"}),
             json_resp(ISSUE_JSON)])
        issue = client.get_issue(7)
        self.assertEqual(issue.number, 7)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.sleeps, [2.0])

    def test_primary_rate_limit_honors_reset_header(self):
        # X-RateLimit-Reset 8 s in the future -> sleep ~8 s (bounded).
        import time as _time
        reset = str(int(_time.time()) + 8)
        client, _ = self.make_client(
            [resp(403, "rate limit exceeded",
                  {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": reset}),
             json_resp(ISSUE_JSON)])
        client.get_issue(7)
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(1.0 <= self.sleeps[0] <= 8.5, self.sleeps)

    def test_5xx_budget_spent_raises_server_error(self):
        client, transport = self.make_client(
            [resp(500, "boom"), resp(500, "boom"), resp(500, "boom")])
        with self.assertRaises(GitHubServerError) as ctx:
            client.get_issue(7)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_rate_limit_budget_spent_raises_rate_limit_error(self):
        client, transport = self.make_client(
            [resp(403, "slow down", {"Retry-After": "1"})] * 3)
        with self.assertRaises(GitHubRateLimitError) as ctx:
            client.get_issue(7)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(transport.requests), 3)
        # The pass is NOT disabled for rate limits — only auth disables.
        self.assertFalse(client.pass_disabled)


class TestAuthDisable(ClientFixture):
    def test_401_disables_pass_logs_once_and_fails_fast(self):
        client, transport = self.make_client(
            [resp(401, "Bad credentials"), resp(200, "{}")])
        with self.assertRaises(GitHubAuthError):
            client.get_issue(7)
        self.assertTrue(client.pass_disabled)
        self.assertEqual(len(self.logs), 1)
        self.assertIn("GITHUB-SYNC-AUTH-ERROR", self.logs[0])
        with self.assertRaises(GitHubSyncDisabledError):
            client.get_issue(8)
        # The second call never touched the transport.
        self.assertEqual(len(transport.requests), 1)

    def test_non_rate_limit_403_is_auth_error_without_retry(self):
        client, transport = self.make_client([resp(403, "Forbidden")])
        with self.assertRaises(GitHubAuthError):
            client.get_issue(7)
        self.assertTrue(client.pass_disabled)
        self.assertEqual(len(transport.requests), 1)

    def test_reset_pass_rearms(self):
        client, transport = self.make_client(
            [resp(401, "Bad credentials"), json_resp(ISSUE_JSON)])
        with self.assertRaises(GitHubAuthError):
            client.get_issue(7)
        client.reset_pass()
        self.assertFalse(client.pass_disabled)
        issue = client.get_issue(7)
        self.assertEqual(issue.number, 7)
        self.assertEqual(len(transport.requests), 2)

    def test_repeated_auth_failures_log_one_error_per_pass(self):
        client, _ = self.make_client(
            [resp(401, "Bad credentials"), resp(401, "Bad credentials")])
        with self.assertRaises(GitHubAuthError):
            client.get_issue(7)
        client.reset_pass()
        with self.assertRaises(GitHubAuthError):
            client.get_issue(7)
        self.assertEqual(len(self.logs), 2)  # one per pass, not per call


class TestOtherFailures(ClientFixture):
    def test_404_raises_plain_api_error_without_retry(self):
        client, transport = self.make_client([resp(404, "Not Found")])
        with self.assertRaises(GitHubApiError) as ctx:
            client.get_issue(99)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(len(transport.requests), 1)
        self.assertFalse(client.pass_disabled)

    def test_transport_timeout_wraps_without_retry(self):
        client, transport = self.make_client(
            [socket.timeout("timed out")])
        with self.assertRaises(GitHubTransportError):
            client.get_issue(7)
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_repo_rejects_before_http(self):
        client, transport = self.make_client([])
        client._config.repo = "just-a-name"
        with self.assertRaises(GitHubApiError):
            client.get_issue(7)
        self.assertEqual(transport.requests, [])


class TestSecretScrubbing(ClientFixture):
    def test_pat_never_appears_in_error_from_echoing_body(self):
        # A hostile/misconfigured response echoes the token in its body.
        client, _ = self.make_client(
            [resp(500, f"token used was {PAT}"), resp(500, f"token {PAT}"),
             resp(500, f"token {PAT}")])
        with self.assertRaises(GitHubServerError) as ctx:
            client.get_issue(7)
        self.assertNotIn(PAT, str(ctx.exception))
        self.assertIn(REDACTED, str(ctx.exception))

    def test_pat_never_appears_in_auth_log_or_error(self):
        client, _ = self.make_client([resp(401, f"bad {PAT}")])
        with self.assertRaises(GitHubAuthError) as ctx:
            client.get_issue(7)
        self.assertNotIn(PAT, str(ctx.exception))
        for line in self.logs:
            self.assertNotIn(PAT, line)

    def test_pat_never_appears_in_transport_error(self):
        client, _ = self.make_client(
            [RuntimeError(f"header rejected: Bearer {PAT}")])
        with self.assertRaises(GitHubTransportError) as ctx:
            client.get_issue(7)
        self.assertNotIn(PAT, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
