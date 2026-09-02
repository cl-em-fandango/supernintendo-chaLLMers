"""Post handoff events to the task's GitHub issue exactly once (spec FR-2.5).

A handoff event is one of three things the harness writes when work changes
hands: a context-cap handover (`ContinuationNote`), a park-with-handoff
(`Handoff` section), or a terminal executive summary (complete/park/fail).
Each one becomes a single issue comment:

    **[stage]** <one-line context: task id, stage, slice/iteration if any>

    <handoff / continuation / executive-summary prose, verbatim>

Duplicate suppression rides on a *content-stable* event id: the issue number
plus a fixed identity for the event — task id, stage, and a hash of the
prose. Never a wall-clock timestamp or an `attempt` counter: those change on
every pass, so a retried pass would compute a new id and re-post. With the
same prose the id is identical, the sidecar's `comment_ids` map already
holds it, and nothing is posted.

State and behavior (CODING_STANDARDS §2): `HandoffEvent` is the shape;
`HandoffCommentPoster` acts. The poster is injected into the three write
sites as a plain callable and swallows every failure itself — a sync error
must never fail a task, lose prose, or break a move (NFR-1). The GitHub
call goes only through the injected client (`external/github_api.py`).
"""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import task_record
from .sync_inbound import find_task

# Length of the event-id digest; plenty to make prose collisions impossible
# in one issue's comment map while keeping sidecars readable.
EVENT_ID_DIGEST_CHARS = 16


@dataclass
class HandoffEvent:
    """One handoff, as the comment poster sees it.

    `stage` is what the header shows (a `Stage`/`TaskStatus` wire string);
    `slice_id`/`iteration` join the context line only when present. The
    event id is derived from issue number + task id + stage + prose, so
    `slice_id` and `iteration` are display data, not identity (FR-2.5).
    """
    task_id: str
    stage: str
    prose: str
    slice_id: str | None = None
    iteration: int | None = None


def event_id(issue_number: int, event: HandoffEvent) -> str:
    """The content-stable id of `event` on `issue_number` (FR-2.5).

    Deterministic across processes and passes: identical prose yields the
    identical id, any prose change yields a different one.
    """
    material = "\0".join((str(issue_number), event.task_id, event.stage,
                          event.prose))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:EVENT_ID_DIGEST_CHARS]


def _context_line(event: HandoffEvent) -> str:
    """The one-line context after the stage marker: task id, stage, and
    slice/iteration only when the event carries them."""
    parts = [f"task {event.task_id}", f"stage {event.stage}"]
    if event.slice_id:
        parts.append(f"slice {event.slice_id}")
    if event.iteration is not None:
        parts.append(f"iteration {event.iteration}")
    return ", ".join(parts)


def comment_body(event: HandoffEvent) -> str:
    """The FR-2.5 comment: bold stage header, context line, verbatim prose."""
    return f"**[{event.stage}]** {_context_line(event)}\n\n{event.prose}"


class HandoffCommentPoster:
    """Posts `HandoffEvent`s to the linked issue, deduped via the record.

    Built once by the composition root and injected into the write sites
    as a callable (`poster(event)`); `__call__` never raises. A task with
    no sidecar linkage has no issue to comment on and is a debug no-op —
    the poster never creates or searches for an issue (that is outbound's
    job, FR-2).
    """

    def __init__(self, api, queue_dir: Path, repo: str,
                 log: Callable[[str], None] = print,
                 verify: bool = False):
        self.api = api
        self.queue_dir = Path(queue_dir)
        self.repo = repo
        self.log = log
        # `verify` re-reads the issue's comments after a post and logs the
        # comment id actually on the server (FR-5 list-comments dedup
        # verification); off by default to keep passes cheap.
        self.verify = verify
        # Handoff events whose post failed (transient API trouble). The
        # targeted per-task sync (FR-3 in-flight rule) drains this queue,
        # so a failed comment is retried at the next handoff, not lost.
        self._pending_events: deque[HandoffEvent] = deque()

    def __call__(self, task_id: str, stage: str, prose: str,
                 slice_id: str | None = None,
                 iteration: int | None = None) -> None:
        """Hook-site entry: post, swallow and log any failure (NFR-1).

        The primitive signature is what the write sites (`task_lifecycle`,
        `continuation`) call, so no workflow module has to import the
        sync layer."""
        event = HandoffEvent(task_id=task_id, stage=stage, prose=prose,
                             slice_id=slice_id, iteration=iteration)
        try:
            self.post(event)
        except Exception as exc:
            self.log(f"  {task_id}: handoff comment failed: {exc}")
            self._pending_events.append(event)

    def retry_pending(self, task_id: str) -> int:
        """Drain the failed-post queue for `task_id`; return posts made.

        The targeted per-task sync (spec FR-3 in-flight rule) calls this
        as its comment step. Deduped like every other post: an event the
        sidecar already records is dropped, an event that fails again is
        requeued for the next sync, and nothing here ever raises (NFR-1).
        """
        posted = 0
        for _ in range(len(self._pending_events)):
            event = self._pending_events.popleft()
            if event.task_id != task_id:
                self._pending_events.append(event)
                continue
            try:
                if self.post(event) is not None:
                    posted += 1
            except Exception as exc:
                self.log(f"  {task_id}: handoff comment retry failed: {exc}")
                self._pending_events.append(event)
        return posted

    def post(self, event: HandoffEvent) -> int | None:
        """Post `event` once; return the comment id, or None when skipped.

        Skips: no task on disk, no linkage for this repo, or an id already
        in the record's `comment_ids` map (retry/repeated pass).
        """
        if find_task(self.queue_dir, event.task_id) is None:
            self.log(f"  {event.task_id}: skip (debug) — handoff comment, "
                     f"task not in any synced location")
            return None
        linkage = task_record.read_linkage(self.queue_dir, event.task_id)
        if linkage is None:
            self.log(f"  {event.task_id}: skip (debug) — handoff comment, "
                     f"no issue linkage")
            return None
        if linkage.repo and linkage.repo != self.repo:
            self.log(f"  {event.task_id}: skip (debug) — handoff comment, "
                     f"linked to {linkage.repo}, not {self.repo}")
            return None
        event_key = event_id(linkage.issue, event)
        if event_key in linkage.comment_ids:
            self.log(f"  {event.task_id}: skip (debug) — handoff comment "
                     f"already posted (#{linkage.issue})")
            return None
        comment = self.api.create_comment(linkage.issue, comment_body(event))
        linkage.comment_ids[event_key] = comment.id
        task_record.write_linkage(self.queue_dir, event.task_id, linkage)
        if self.verify:
            self._verify(linkage.issue, comment.id)
        self.log(f"  {event.task_id}: handoff comment posted to "
                 f"gh #{linkage.issue} (comment {comment.id})")
        return comment.id

    def _verify(self, number: int, comment_id: int) -> None:
        """Confirm the posted comment is on the issue (FR-5 dedup check)."""
        posted = any(comment.id == comment_id
                     for comment in self.api.list_comments(number))
        if not posted:
            self.log(f"  verify (debug): comment {comment_id} not found "
                     f"on gh #{number}")
