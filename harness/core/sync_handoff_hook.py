"""The handoff sync hook every handoff write site calls (spec FR-3, NFR-1).

Slice 6 put the handoff-comment poster at the three write sites
(`continuation.write_note`, the `task_lifecycle` `Handoff` section, and the
terminal executive-summary writers). This hook wraps that poster with the
pass the in-flight rule demands: after the handoff prose is mirrored onto
the issue, a task a session is still working on gets a targeted per-task
sync plus a full inbound pass (through
`SyncEngine.on_stage_change(task_id)`), so an external halt is noticed
promptly.

Two guards keep the write sites safe and the log honest:

* everything is wrapped in try/except-and-log — a sync failure must never
  lose the handoff prose, fail a move, or escape into the pipeline (NFR-1);
* a task that is *not* in flight is not passed here. Every terminal handoff
  (park/fail/complete) is part of a queue move, and the move's own
  stage-change hook already runs the pass; running one here too would log
  the pass summary twice for a single event (NFR-4). The handoff comment
  still posts — only the extra pass is skipped.

The hook is callable with the primitive signature the write sites already
use, `(task_id, stage, prose, slice_id, iteration)`, so workflow modules
keep treating the handoff sync as an opaque callable and import nothing
from the sync layer (CODING_STANDARDS §4). `engine` and `poster` come from
the composition root; `None` wiring never reaches this class — where the
hook is built, an unconfigured GitHub means no hook at all (FR-0.1, NFR-2).
"""
from __future__ import annotations

from typing import Callable


class HandoffSyncHook:
    """Post the handoff comment, then sync the in-flight task (FR-3).

    * `poster` — the `HandoffCommentPoster`; it swallows its own failures
      and queues a failed post for the targeted pass to retry.
    * `engine` — the `SyncEngine` dispatcher; `on_stage_change(task_id)`
      runs the targeted sync + full inbound pass for an in-flight task.

    `__call__` never raises (NFR-1) and logs the pass summary line exactly
    once per pass it runs (NFR-4).
    """

    def __init__(self, engine, poster,
                 log: Callable[[str], None] = print):
        self.engine = engine
        self.poster = poster
        self.log = log

    def __call__(self, task_id: str, stage: str, prose: str,
                 slice_id: str | None = None,
                 iteration: int | None = None) -> None:
        self._post(task_id, stage, prose, slice_id, iteration)
        self._sync(task_id)

    def _post(self, task_id: str, stage: str, prose: str,
              slice_id: str | None, iteration: int | None) -> None:
        """Mirror the handoff onto the issue; a broken poster dies here."""
        try:
            self.poster(task_id, stage, prose, slice_id, iteration)
        except Exception as exc:  # noqa: BLE001 - NFR-1: prose outlives sync
            self.log(f"  ⚠ github handoff comment failed: "
                     f"{type(exc).__name__}: {exc}")

    def _sync(self, task_id: str) -> None:
        """Run the in-flight pass for `task_id`; never raise (NFR-1)."""
        if not self.engine.is_in_flight(task_id):
            # The move that settled the task fires its own stage-change
            # hook; a pass here would log the summary twice for one event.
            self.log(f"  handoff sync (debug): {task_id} is not in flight — "
                     f"the stage-change hook owns this pass")
            return
        try:
            report = self.engine.on_stage_change(task_id)
        except Exception as exc:  # noqa: BLE001 - NFR-1: sync never breaks a handoff
            self.log(f"  ⚠ github handoff sync failed: "
                     f"{type(exc).__name__}: {exc}")
            return
        self.log(report.summary_line())
