"""Sync orchestration: the passes over the queue and when to run them
(spec FR-3).

Two pieces live here, both about *when* and *how much* to sync:

* `sync_pass()` — one full two-way pass, the manual `harness sync` entry;
* `SyncEngine.on_stage_change()` — the dispatcher the lifecycle and handoff
  hook sites call: a full pass for a stage change, and a targeted per-task
  sync plus a full inbound pass for a handoff on an in-flight task.

A full pass is two ordered phases:

* phase 1 — inbound: GitHub issues -> queue (`sync_inbound`);
* phase 2 — outbound: queue -> GitHub issues (`sync_outbound`).

`SyncReport` is the pass's observable outcome; this module owns no policy
beyond phase order. The GitHub client is injected by the composition root
(`composition.build_github_api`) — no globals, config flows one way
(CODING_STANDARDS §4/§5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from external.github_api import GitHubApiError

from ..workflow.task_lifecycle import CLAIMED_LOCATION, TaskLifecycle
from .sync_inbound import InboundParams, find_task, run_inbound
from .sync_outbound import OutboundParams, run_outbound, sync_one_task

# The queue locations of a task a session is working on right now (spec
# FR-3 in-flight rule): a handoff for one of these syncs that task only,
# plus a full inbound pass.
IN_FLIGHT_LOCATIONS = (CLAIMED_LOCATION, "active")


@dataclass
class SyncReport:
    """Counts for one completed sync pass (spec FR-3 manual summary).

    Every field counts items the pass *acted on*; outbound fields are
    filled by their slices and read zero until then.
    """
    imported: int = 0
    parked: int = 0
    deleted: int = 0
    created_issues: int = 0
    label_updates: int = 0
    # Handoff comments are event-driven (Slice 6 posts them at the write
    # sites, not during a pass); a pass itself never posts one, so this
    # stays 0 until a pass-owned comment action exists.
    comments_posted: int = 0
    aborted: bool = False
    abort_reason: str = ""

    def summary_line(self) -> str:
        """The one-line pass summary (NFR-4)."""
        line = (f"github sync: imported={self.imported} "
                f"parked={self.parked} deleted={self.deleted} "
                f"created_issues={self.created_issues} "
                f"label_updates={self.label_updates} "
                f"comments_posted={self.comments_posted}")
        if self.aborted:
            line += f" ABORTED ({self.abort_reason})"
        return line


def _run_phases(phases: tuple, report: SyncReport,
                log: Callable[[str], None]) -> None:
    """Run ordered phases into `report`, aborting cleanly on a GitHub error.

    A spent rate-limit budget or an auth disable stops the pass here (spec
    edge 9, FR-5): the remaining phases are skipped, the counts gathered
    so far stand, and nothing raises to the caller — unfinished work rolls
    to the next pass. Every other failure stays per-item inside its phase
    (NFR-1).
    """
    for phase in phases:
        try:
            phase()
        except GitHubApiError as exc:  # messages are PAT-scrubbed (FR-0.2)
            report.aborted = True
            report.abort_reason = str(exc)
            log(f"github sync pass aborted: {exc}")
            return


def sync_pass(cfg, api, log: Callable[[str], None] = print) -> SyncReport:
    """Run one full two-way sync pass: phase 1 inbound, then phase 2
    outbound. `api` is a `GitHubApiClient` (or any object with the same
    read/write operations, injected for tests).
    """
    report = SyncReport()
    lifecycle = TaskLifecycle(cfg, log)

    def inbound() -> None:
        result = _run_inbound(cfg, api, log, lifecycle)
        report.imported = result.imported
        report.parked += result.parked
        report.deleted = result.deleted

    def outbound() -> None:
        result = _run_outbound(cfg, api, log, lifecycle)
        report.created_issues = result.created_issues
        report.label_updates = result.label_updates
        report.parked += result.parked

    reset = getattr(api, "reset_pass", None)
    if reset is not None:
        reset()  # an auth disable lasts one pass, no longer (FR-5)
    _run_phases((inbound, outbound), report, log)
    return report


def _run_inbound(cfg, api, log: Callable[[str], None],
                 lifecycle: TaskLifecycle):
    """Phase 1 with its parameters object built at the call site."""
    return run_inbound(api, InboundParams(
        queue_dir=cfg.queue_dir, repo=cfg.github_repo, log=log,
        work_dir=cfg.work_dir, lifecycle=lifecycle))


def _run_outbound(cfg, api, log: Callable[[str], None],
                  lifecycle: TaskLifecycle):
    """Phase 2 with its parameters object built at the call site."""
    return run_outbound(api, OutboundParams(
        queue_dir=cfg.queue_dir, repo=cfg.github_repo, log=log,
        lifecycle=lifecycle))


class SyncEngine:
    """The sync dispatcher every trigger site calls (spec FR-3).

    One entry point, `on_stage_change()`, picks the pass the trigger needs:

    * no task id — a manual `harness sync` or a stage change: one full
      two-way pass;
    * a task id whose queue location is in-flight (`claimed/`, `active/`)
      — a handoff while the session works: a targeted sync for that task
      (its state label plus any handoff comment still unposted) followed
      by a full inbound pass, so an external halt is noticed promptly;
    * any other task id — the task is settled, so a full pass.

    The GitHub client and the handoff-comment poster are injected by the
    composition root; the engine owns no global and the caller decides
    whether sync is enabled at all (FR-0.1, NFR-2).
    """

    def __init__(self, cfg, api, log: Callable[[str], None] = print,
                 comment_poster=None):
        self.cfg = cfg
        self.api = api
        self.log = log
        # `HandoffCommentPoster` (or None in wiring that cannot comment);
        # the targeted pass drains its failed-post queue for the task.
        self.comment_poster = comment_poster

    def on_stage_change(self, task_id: str | None = None) -> SyncReport:
        """Run the pass this trigger calls for; never raises on GitHub
        errors (they are reported, spec edge 9). Hook sites still wrap
        this in try/except for every other failure (NFR-1)."""
        if task_id is not None and self.is_in_flight(task_id):
            return self._in_flight_pass(task_id)
        return self.full_pass()

    def full_pass(self) -> SyncReport:
        """One full two-way pass: inbound, then outbound."""
        return sync_pass(self.cfg, self.api, log=self.log)

    def is_in_flight(self, task_id: str) -> bool:
        """True when `task_id` sits in a location a session works from."""
        entry = find_task(self.cfg.queue_dir, task_id)
        return entry is not None and entry.location in IN_FLIGHT_LOCATIONS

    def _in_flight_pass(self, task_id: str) -> SyncReport:
        """Targeted sync for one in-flight task, then a full inbound pass."""
        report = SyncReport()
        lifecycle = TaskLifecycle(self.cfg, self.log)

        def targeted() -> None:
            result = sync_one_task(self.api, task_id, OutboundParams(
                queue_dir=self.cfg.queue_dir, repo=self.cfg.github_repo,
                log=self.log, lifecycle=lifecycle))
            report.created_issues = result.created_issues
            report.label_updates = result.label_updates
            report.parked += result.parked
            if self.comment_poster is not None:
                report.comments_posted = \
                    self.comment_poster.retry_pending(task_id)

        def inbound() -> None:
            result = _run_inbound(self.cfg, self.api, self.log, lifecycle)
            report.imported = result.imported
            report.parked += result.parked
            report.deleted = result.deleted

        reset = getattr(self.api, "reset_pass", None)
        if reset is not None:
            reset()  # an auth disable lasts one pass, no longer (FR-5)
        _run_phases((targeted, inbound), report, self.log)
        return report
