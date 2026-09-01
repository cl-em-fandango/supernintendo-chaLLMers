"""GitHub REST API client — the single HTTP boundary (spec FR-5, NFR-3).

Nothing outside this file performs HTTP for the queue-sync feature. The
client speaks the small typed vocabulary of the sync (Issue, Comment,
Label dataclasses below) and hides every request, retry and error string
behind it. The transport is injected (a callable taking
``(method, url, headers, body_json)`` and returning an ``HttpResponse``),
so tests run in-process against canned JSON fixtures with no network
(NFR-5); the default transport is stdlib ``urllib`` only (spec §9).

Behaviors owned here (spec FR-5):
  * fixed headers on every request: ``Authorization: Bearer <pat>``,
    ``Accept: application/vnd.github+json``,
    ``X-GitHub-Api-Version: 2022-11-28``, harness ``User-Agent``;
  * 30 s per-request timeout (in the default transport);
  * retries ≤ 3 on 5xx and rate-limit responses, honoring
    ``Retry-After`` / ``X-RateLimit-Reset`` with bounded backoff;
  * on 401 / non-rate-limit 403 the sync is disabled for the remainder of
    the pass: one clear auth error is logged, the raising call fails with
    ``GitHubAuthError``, and every later call fails fast with
    ``GitHubSyncDisabledError`` without touching the transport.
    ``reset_pass()`` re-arms the client for the next pass;
  * the PAT never appears in a raised error string or a log line — every
    message built from response data is scrubbed first (FR-0.2).

State and behavior split (CODING_STANDARDS §2): the dataclasses and enums
below describe the shape; ``GitHubApiClient`` acts on them. Label
mutations are add/remove only — the client deliberately offers no
replace-all (PUT labels) op, so a human's labels cannot be stripped
(spec FR-2.4).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence

# ---------------------------------------------------------------------------
# Constants (spec FR-5)
# ---------------------------------------------------------------------------

DEFAULT_API_BASE_URL = "https://api.github.com"
REQUEST_TIMEOUT_S = 30
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
MAX_RETRY_DELAY_S = 60.0
PER_PAGE = 100
USER_AGENT = "harness-queue-sync/1.0"
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"
# Replaces the PAT inside any message that could echo it back (FR-0.2).
REDACTED = "***"
# How much response body an error message may quote.
_ERROR_BODY_CHARS = 200


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GitHubApiError(RuntimeError):
    """Base for every failure the client raises. `status` is the HTTP
    status (0 when no HTTP response was received)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class GitHubTransportError(GitHubApiError):
    """The transport itself failed (timeout, DNS, connection refused).
    Not retried here — the hook sites swallow it and a later pass
    reconciles (spec edge 3)."""


class GitHubServerError(GitHubApiError):
    """5xx persisted after the retry budget was spent."""


class GitHubRateLimitError(GitHubApiError):
    """A rate limit persisted after the retry budget was spent; the
    calling pass aborts cleanly and rolls work to the next pass
    (spec edge 9)."""


class GitHubAuthError(GitHubApiError):
    """401, or a 403 that is not a rate limit. Disables the sync for the
    remainder of the pass (spec FR-5)."""


class GitHubSyncDisabledError(GitHubApiError):
    """Raised by calls after an auth failure: the pass is disabled and
    no HTTP is attempted."""


# The errors that end a sync pass instead of sinking one item: a spent
# rate-limit budget (spec edge 9) and the post-auth-failure disable
# (FR-5). Everything else stays a per-item, logged-and-skipped failure
# (NFR-1).
PASS_ABORT_ERRORS: tuple[type[GitHubApiError], ...] = (
    GitHubRateLimitError, GitHubSyncDisabledError)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

class IssueState(str, Enum):
    """Issue state as GitHub reports it; strings only at the wire edge."""
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Label:
    """One label on an issue. The sync cares about the name only."""
    name: str


@dataclass
class Issue:
    """One GitHub issue, trimmed to what the sync reads."""
    number: int
    title: str
    body: str
    state: IssueState
    labels: tuple[Label, ...]
    html_url: str


@dataclass
class Comment:
    """One issue comment (FR-2.5 posting and dedup verification)."""
    id: int
    body: str
    html_url: str


@dataclass
class GitHubApiConfig:
    """The FR-4 knobs as one explicit parameters object. Built by the
    composition root from `Config`; `repo` is `owner/name`."""
    pat: str
    repo: str
    base_url: str = DEFAULT_API_BASE_URL


@dataclass
class HttpResponse:
    """One transport-level response. Headers keep whatever case the
    sender used; look up with `header()`."""
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        want = name.lower()
        for key, value in self.headers.items():
            if key.lower() == want:
                return value
        return None


# (method, url, headers, body_json) -> HttpResponse. Injected in tests.
Transport = Callable[[str, str, Mapping[str, str], "str | None"], HttpResponse]


def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: str | None) -> HttpResponse:
    """The default transport: stdlib urllib, 30 s timeout (spec FR-5).

    HTTP error statuses (4xx/5xx) come back as `HttpResponse` — the
    client's retry/auth logic inspects them. Anything else (URLError,
    socket timeout) propagates and is wrapped as `GitHubTransportError`.
    """
    data = body.encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data,
                                     headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            return HttpResponse(int(getattr(resp, "status", 200)),
                                dict(resp.headers),
                                resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp is not None else b""
        return HttpResponse(int(exc.code), dict(exc.headers or {}),
                            raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# JSON -> dataclass parsing
# ---------------------------------------------------------------------------

def _label_from_json(item: object) -> Label:
    if isinstance(item, dict):
        return Label(str(item.get("name", "")))
    return Label(str(item))


def _issue_from_json(item: Mapping) -> Issue:
    return Issue(
        number=int(item.get("number", 0)),
        title=str(item.get("title", "")),
        body=str(item.get("body") or ""),
        state=(IssueState.OPEN if str(item.get("state", "open")) == "open"
               else IssueState.CLOSED),
        labels=tuple(_label_from_json(label)
                     for label in item.get("labels") or ()),
        html_url=str(item.get("html_url") or ""),
    )


def _comment_from_json(item: Mapping) -> Comment:
    return Comment(
        id=int(item.get("id", 0)),
        body=str(item.get("body") or ""),
        html_url=str(item.get("html_url") or ""),
    )


def _next_link_url(link_header: str | None) -> str | None:
    """The `rel="next"` URL of a `Link` header, else None."""
    if not link_header:
        return None
    for part in link_header.split(","):
        pieces = part.split(";")
        if len(pieces) < 2:
            continue
        if 'rel="next"' in pieces[1].replace(" ", ""):
            target = pieces[0].strip()
            if target.startswith("<") and target.endswith(">"):
                return target[1:-1]
    return None


def _is_rate_limited(response: HttpResponse) -> bool:
    """True for primary (X-RateLimit-Remaining: 0) and secondary
    (Retry-After) rate limits, plus plain 429 (spec FR-5)."""
    if response.status == 429:
        return True
    if response.status == 403:
        if response.header("Retry-After"):
            return True
        return response.header("X-RateLimit-Remaining") == "0"
    return False


def _is_auth_failure(response: HttpResponse) -> bool:
    """401, or a 403 that is not a rate limit (spec FR-5)."""
    if response.status == 401:
        return True
    return response.status == 403 and not _is_rate_limited(response)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class GitHubApiClient:
    """Typed operations over the GitHub REST API (spec FR-5).

    Constructed once per pass by the composition root with an explicit
    `GitHubApiConfig`; `transport`, `sleep` and `log` are injectable so
    tests never touch the network or the clock (NFR-5). An auth failure
    disables the instance for the rest of the pass; `reset_pass()`
    re-arms it for the next one.
    """

    def __init__(self, config: GitHubApiConfig, *,
                 transport: Transport | None = None,
                 log: Callable[[str], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._config = config
        self._transport = transport or urllib_transport
        self._log = log or (lambda message: None)
        self._sleep = sleep
        self._pass_disabled = False

    @property
    def pass_disabled(self) -> bool:
        """True after an auth failure: the sync is inert until
        `reset_pass()`."""
        return self._pass_disabled

    def reset_pass(self) -> None:
        """Re-arm the client for a new sync pass (spec FR-5: the auth
        disable lasts for the remainder of the pass, no longer)."""
        self._pass_disabled = False

    # -- issue operations --------------------------------------------------

    def list_issues(self, labels: Sequence[str] = (),
                    state: IssueState = IssueState.OPEN) -> list[Issue]:
        """Open (or closed) issues carrying all `labels`, paginated
        (FR-1, FR-5)."""
        params: dict[str, str] = {"state": state.value}
        if labels:
            params["labels"] = ",".join(labels)
        items = self._get_paginated(f"/repos/{self._repo_path()}/issues",
                                    params)
        return [_issue_from_json(item) for item in items]

    def get_issue(self, number: int) -> Issue:
        item = self._json("GET", f"/repos/{self._repo_path()}/issues/{number}")
        return _issue_from_json(item)

    def create_issue(self, title: str, body: str) -> Issue:
        """POST a new issue (FR-2.3)."""
        item = self._json(
            "POST", f"/repos/{self._repo_path()}/issues",
            body_obj={"title": title, "body": body})
        return _issue_from_json(item)

    def close_issue(self, number: int) -> Issue:
        """Close the issue (FR-1.4 anti-loop)."""
        item = self._json(
            "PATCH", f"/repos/{self._repo_path()}/issues/{number}",
            body_obj={"state": "closed"})
        return _issue_from_json(item)

    # -- label operations (add/remove only, never replace-all; FR-2.4) -----

    def list_labels(self, number: int) -> list[Label]:
        items = self._json(
            "GET", f"/repos/{self._repo_path()}/issues/{number}/labels")
        return [_label_from_json(item) for item in items]

    def add_labels(self, number: int, labels: Sequence[str]) -> list[Label]:
        items = self._json(
            "POST", f"/repos/{self._repo_path()}/issues/{number}/labels",
            body_obj={"labels": list(labels)})
        return [_label_from_json(item) for item in items]

    def remove_label(self, number: int, name: str) -> None:
        self._request(
            "DELETE",
            self._url(f"/repos/{self._repo_path()}/issues/{number}/labels/"
                      f"{urllib.parse.quote(name, safe='')}", None))

    # -- comment operations --------------------------------------------------

    def create_comment(self, number: int, body: str) -> Comment:
        item = self._json(
            "POST", f"/repos/{self._repo_path()}/issues/{number}/comments",
            body_obj={"body": body})
        return _comment_from_json(item)

    def list_comments(self, number: int) -> list[Comment]:
        items = self._get_paginated(
            f"/repos/{self._repo_path()}/issues/{number}/comments", {})
        return [_comment_from_json(item) for item in items]

    # -- request plumbing ----------------------------------------------------

    def _repo_path(self) -> str:
        owner, _, name = self._config.repo.partition("/")
        if not owner or not name:
            raise GitHubApiError(
                f"githubRepo must be 'owner/name', got {self._config.repo!r}")
        return (urllib.parse.quote(owner, safe="") + "/"
                + urllib.parse.quote(name, safe=""))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.pat}",
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _url(self, path: str, params: Mapping[str, str] | None) -> str:
        url = self._config.base_url.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(dict(params))
        return url

    def _json(self, method: str, path: str,
              body_obj: object = None) -> object:
        response = self._request(method, self._url(path, None),
                                 body=json.dumps(body_obj)
                                 if body_obj is not None else None)
        return json.loads(response.body or "null")

    def _get_paginated(self, path: str,
                       params: Mapping[str, str]) -> list[dict]:
        """Walk a list endpoint: follow `Link` rel="next" when present,
        otherwise keep paging while a full page comes back."""
        page_params = dict(params)
        page_params.setdefault("per_page", str(PER_PAGE))
        url = self._url(path, page_params)
        items: list[dict] = []
        while True:
            response = self._request("GET", url)
            page = json.loads(response.body or "[]")
            items.extend(page)
            following = _next_link_url(response.header("Link"))
            if following:
                url = following
                continue
            if len(page) == PER_PAGE:
                page_params["page"] = str(int(page_params.get("page", "1")) + 1)
                url = self._url(path, page_params)
                continue
            return items

    def _request(self, method: str, url: str,
                 body: str | None = None) -> HttpResponse:
        """One logical request with the FR-5 retry/auth policy."""
        if self._pass_disabled:
            raise GitHubSyncDisabledError(
                "github sync disabled for this pass after an auth failure")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._transport(method, url, self._headers(), body)
            except Exception as exc:
                raise GitHubTransportError(self._scrub(
                    f"{method} {url} transport failure: {exc}")) from exc
            if response.status < 400:
                return response
            if (response.status >= 500 or _is_rate_limited(response)) \
                    and attempt < MAX_ATTEMPTS:
                self._sleep(self._retry_delay_s(response, attempt))
                continue
            return self._fail(method, url, response)
        raise GitHubApiError(f"{method} {url} failed after "
                             f"{MAX_ATTEMPTS} attempts")  # unreachable

    def _fail(self, method: str, url: str, response: HttpResponse):
        """Turn a terminal error response into the typed exception."""
        status = response.status
        snippet = response.body[:_ERROR_BODY_CHARS].replace("\n", " ")
        if _is_auth_failure(response):
            self._disable_pass(status)
            raise GitHubAuthError(
                self._scrub(f"github auth failed: {method} {url} -> "
                            f"HTTP {status}: {snippet}"), status)
        if _is_rate_limited(response):
            raise GitHubRateLimitError(
                self._scrub(f"github rate limit exceeded: {method} {url} -> "
                            f"HTTP {status}: {snippet}"), status)
        if status >= 500:
            raise GitHubServerError(
                self._scrub(f"github server error: {method} {url} -> "
                            f"HTTP {status}: {snippet}"), status)
        raise GitHubApiError(
            self._scrub(f"github request failed: {method} {url} -> "
                        f"HTTP {status}: {snippet}"), status)

    def _disable_pass(self, status: int) -> None:
        """Disable the sync for the rest of the pass; log exactly one
        clear auth error (spec FR-5, NFR-4)."""
        if self._pass_disabled:
            return
        self._pass_disabled = True
        self._log(self._scrub(
            f"GITHUB-SYNC-AUTH-ERROR HTTP {status}: the sync is disabled "
            f"for the remainder of this pass"))

    def _retry_delay_s(self, response: HttpResponse, attempt: int) -> float:
        """Seconds to wait before the next attempt: `Retry-After`, then
        `X-RateLimit-Reset`, then bounded exponential backoff."""
        retry_after = response.header("Retry-After")
        if retry_after:
            return self._clamped_delay(retry_after)
        reset = response.header("X-RateLimit-Reset")
        if reset and response.header("X-RateLimit-Remaining") == "0":
            try:
                return self._clamped_delay(float(reset) - time.time())
            except ValueError:
                pass
        return self._clamped_delay(BACKOFF_BASE_S * (2 ** (attempt - 1)))

    @staticmethod
    def _clamped_delay(seconds: float) -> float:
        return max(0.0, min(float(seconds), MAX_RETRY_DELAY_S))

    def _scrub(self, message: str) -> str:
        """Remove the PAT from any message before it can reach a log or
        an exception string (FR-0.2)."""
        pat = self._config.pat
        if pat and pat in message:
            return message.replace(pat, REDACTED)
        return message
