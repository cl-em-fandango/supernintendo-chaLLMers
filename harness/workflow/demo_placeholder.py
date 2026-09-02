"""Placeholder app generation and the pre-spec placeholder deploy hook.

Demo spec FR-2: when the pipeline claims a demo task, before the spec
stage runs, a minimal single-page app acknowledging the request is
deployed to the Pages branch so the user sees their request is in flight.
This is the no-build variant of FR-2.2 — a static `index.html` that the
Slice 4 deployer publishes verbatim, so the placeholder works even when
the npm environment is unavailable.

`DemoPlaceholderHook` is the callable the pipeline invokes. It is built by
the composition root only when `demo.enabled` and GitHub sync are both
configured (FR-6.2); it never raises (FR-2.3): a failure is logged and
commented on the issue, and the pipeline continues into spec. The final
deployment (a later slice) replaces the placeholder in the same app
directory.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from external.demo_deploy import (
    DemoDeployRequest,
    origin_url_from_repo,
    publish_artifacts,
)

from ..core.sync_sidecar import resolve_linkage
from .demo_app_name import resolve_app_name
from .demo_pages_url import pages_url

# The placeholder is a single static page — no build step (FR-2.2).
PLACEHOLDER_FILE_NAME = "index.html"

# Ownership marker: the placeholder page carries the source issue number
# in an HTML comment so the final generation (FR-2.4) can tell "this
# bare-named directory is *our* placeholder" from "another app already
# owns this name" and reuse the placeholder directory instead of
# clobbering the other app (edge case 8).
ISSUE_MARKER_PATTERN = re.compile(r"<!-- demo-issue: (\d+) -->")

# FR-2.2: the visible text acknowledges the specific request.
IN_FLIGHT_SENTENCE = "Your request '{title}' is in flight. The app is being built."

# FR-8.2 success comment; FR-2.3 failure comment.
PLACEHOLDER_SUCCESS_COMMENT = "Placeholder deployed — {url}"
PLACEHOLDER_FAILURE_COMMENT = "placeholder deployment failed: {reason}"


def render_placeholder_page(title: str, issue_number: int | None = None) -> str:
    """The placeholder HTML acknowledging `title` (data, never markup).

    The title arrives from GitHub — it is escaped, so a title carrying
    HTML or prompt-injection text renders as inert text (edge case 5).
    `issue_number` stamps the ownership marker (see `ISSUE_MARKER_PATTERN`).
    """
    clean = str(title).strip()
    safe_title = html.escape(clean)
    sentence = html.escape(IN_FLIGHT_SENTENCE.format(title=clean))
    marker = (f"<!-- demo-issue: {int(issue_number)} -->\n"
              if issue_number is not None else "")
    return (
        "<!DOCTYPE html>\n"
        f"{marker}"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{safe_title}</h1>\n"
        f"<p>{sentence}</p>\n"
        "</body>\n"
        "</html>\n"
    )


def write_placeholder_app(apps_dir: Path, app_name: str, title: str,
                          issue_number: int | None = None) -> Path:
    """Write the placeholder app into `apps_dir/<app_name>`; return its dir.

    The directory doubles as the artifact directory handed to the
    deployer: the static page needs no build, so what is written here is
    exactly what lands in `docs/`. `issue_number` stamps the ownership
    marker read back by `placeholder_issue_of`.
    """
    app_dir = Path(apps_dir) / app_name
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / PLACEHOLDER_FILE_NAME).write_text(
        render_placeholder_page(title, issue_number), encoding="utf-8")
    return app_dir


def placeholder_issue_of(app_dir: Path) -> int | None:
    """The issue number owning `app_dir`, or None when unstamped.

    Reads the placeholder page's ownership marker (see
    `ISSUE_MARKER_PATTERN`). A directory whose page was replaced by the
    final generation, or that never held a placeholder, reports None —
    callers treat None as "not ours" and never clobber it.
    """
    page = Path(app_dir) / PLACEHOLDER_FILE_NAME
    if not page.is_file():
        return None
    match = ISSUE_MARKER_PATTERN.search(
        page.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class PlaceholderDeployParams:
    """Everything the placeholder hook needs, as one parameters object.

    Built by the composition root from `Config` + `DemoConfig`; the hook
    holds no globals (CODING_STANDARDS §5). `harness_repo` is the harness
    workdir whose local trunk the deployer refreshes from.
    """

    queue_dir: Path
    apps_dir: str
    harness_repo: Path
    deploy_dir: Path
    deploy_branch: str
    trunk_branch: str
    docs_dir: str


class DemoPlaceholderHook:
    """The pre-spec hook: generate + deploy the placeholder, comment (FR-2).

    Called by `Pipeline.process` with the claimed demo task and its
    workdir. Contract: never raises — every failure path ends in a log
    line plus a `placeholder deployment failed: <reason>` comment on the
    source issue (FR-2.3), and the pipeline proceeds into spec.

    `deployer` and `origin_resolver` are injectable so tests drive the
    hook with a fake publication and a fake origin; the defaults are the
    real Slice 4 deployer and the harness repo's own `origin`.
    """

    def __init__(self, params: PlaceholderDeployParams, api, *,
                 deployer=publish_artifacts,
                 origin_resolver=origin_url_from_repo, log=print):
        self.params = params
        self.api = api
        self.deployer = deployer
        self.origin_resolver = origin_resolver
        self.log = log

    def __call__(self, task, workdir: Path) -> None:
        """Deploy the placeholder for one claimed demo task; never raise."""
        issue: int | None = None
        try:
            linkage = self._linkage(task.id)
            if linkage is None:
                self.log(f"  ⚠ placeholder: task {task.id} has no GitHub "
                         f"linkage; nothing deployed")
                return
            issue = linkage.issue
            title = self.api.get_issue(issue).title
            app_dir = write_placeholder_app(
                Path(workdir) / self.params.apps_dir,
                resolve_app_name(title, issue,
                                 Path(workdir) / self.params.apps_dir),
                title, issue)
            self._deploy(app_dir)
            self.api.create_comment(issue, PLACEHOLDER_SUCCESS_COMMENT.format(
                url=pages_url(linkage.repo)))
        except Exception as exc:  # noqa: BLE001 - FR-2.3: never block spec
            self.log(f"  ⚠ placeholder deployment failed: {exc}")
            self._comment_failure(issue, exc)

    # --- internals ----------------------------------------------------

    def _linkage(self, task_id: str):
        """The task's GitHub linkage, wherever its sidecar currently is.

        The hook fires while the task is active and its sidecar may
        still sit beside the staged claim file — `resolve_linkage`
        checks the task-dir and task-file shapes (FR-1.4)."""
        return resolve_linkage(self.params.queue_dir, task_id)

    def _deploy(self, app_dir: Path) -> None:
        """Publish the placeholder directory through the deployer (FR-5)."""
        harness_repo = Path(self.params.harness_repo)
        self.deployer(DemoDeployRequest(
            harness_repo=harness_repo,
            deploy_dir=Path(self.params.deploy_dir),
            origin_url=self.origin_resolver(harness_repo),
            deploy_branch=self.params.deploy_branch,
            trunk_branch=self.params.trunk_branch,
            docs_dir=self.params.docs_dir,
            artifacts_dir=app_dir,
            log=self.log))

    def _comment_failure(self, issue: int | None, exc: Exception) -> None:
        """Post the FR-2.3 failure comment; a failing comment dies here too."""
        if issue is None:
            return
        try:
            self.api.create_comment(
                issue, PLACEHOLDER_FAILURE_COMMENT.format(reason=exc))
        except Exception as comment_exc:  # noqa: BLE001
            self.log(f"  ⚠ placeholder failure comment not posted: "
                     f"{comment_exc}")
