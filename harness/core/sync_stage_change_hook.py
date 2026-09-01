"""The stage-change sync hook every transition site calls (spec FR-3, NFR-1).

Lifecycle and workflow modules must not fail a task because GitHub was
unreachable, so the hook wraps the whole dispatcher call: a raising engine
is logged and swallowed, never propagated. It also emits the pass summary
line exactly once per completed pass (NFR-4) — the dispatcher reports, the
hook prints, so a hook-triggered pass and a manual `harness sync` produce
the same one-line record.

`engine` is the `SyncEngine` from the composition root; `None` means GitHub
is unconfigured and the hook does nothing at all (FR-0.1, NFR-2). The hook
takes the engine as an argument rather than reaching for it, so no module
here holds a global (CODING_STANDARDS §5).
"""
from __future__ import annotations

from typing import Callable


def run_stage_change_hook(engine, task_id: str | None = None,
                          log: Callable[[str], None] = print) -> None:
    """Run the sync pass this trigger calls for; never raise (NFR-1)."""
    if engine is None:
        return
    try:
        report = engine.on_stage_change(task_id)
    except Exception as exc:  # noqa: BLE001 - NFR-1: any sync failure dies here
        log(f"  ⚠ github sync hook failed: {type(exc).__name__}: {exc}")
        return
    log(report.summary_line())
