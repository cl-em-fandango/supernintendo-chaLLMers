"""Command handlers for the harness CLI."""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..workflow.autonomous import AutonomousGenerator
from ..workflow.continue_fresh import fresh_restart, resume_in_flight
from ..workflow.resume import resume_task
from ..workflow.task_lifecycle import CLAIMED_LOCATION, QUEUE_LOCATIONS_ALL
from ..core.board import (TERMINAL_LOCATIONS, BoardSummary, BoardTask,
                          LocationBoard, aggregate_stats, classify_origin,
                          collapse_task_stats, render_board)
from ..core.claim_metadata import OWNER_UNKNOWN, read_metadata
from ..core.providers import Task
from ..core.stats import render_report, render_task_journey
from ..composition import build

# Shown under the status table while claims exist. Names the command that
# clears them (it exists now — T12), so the line is something to type.
CLAIMS_STRANDED_WARNING = ("⚠ {count} claimed tasks: nothing will process them "
                           "until they are requeued (harness.py requeue-claims).")

# A claim older than this is stranded, not in-flight. Module constant, so both
# entrypoints and the operator command agree on one number; env-overridable for
# a box whose sessions legitimately run longer.
CLAIM_STALE_HOURS = float(os.environ.get("CLAIM_STALE_HOURS", "6.0"))

# Entropy in a generated owner id, in bytes. Four is 4 billion values: enough
# that two invocations of the same command in the same process never collide,
# short enough for an id an operator reads in `claimed/`.
_OWNER_TOKEN_BYTES = 4


def _new_owner_id(command: str) -> str:
    """The claim-ownership id one command invocation holds for its whole life.

    Generated once, at the top of a run command, and passed to every claim that
    invocation takes and every claim it hands back (see
    `providers.fetch_pending(claim=True, owner=...)` and
    `providers.requeue_claim(..., owner=...)`). A claim therefore carries the id
    of the invocation that took it, and cleanup can only move claims this
    process took for itself — a peer run's claim, or a pre-ownership claim, is
    refused by the provider rather than silently stolen.

    `<command>-<pid>-<token>`: the command names the id for a human reading
    `claimed/`, the pid separates invocations that share a command, and the
    token separates two invocations of the same command in the same process.
    """
    return f"{command}-{os.getpid()}-{uuid.uuid4().hex[:_OWNER_TOKEN_BYTES]}"


def _slug(name: str) -> str:
    return (re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")[:60] or "task")


def cmd_run_task(file: str, fresh: bool = False, continue_: bool = False) -> int:
    cfg, store, runner, provider, pipeline, log = build()
    task = Task(id=_slug(Path(file).stem), body=Path(file).read_text(),
                source=f"cli:{file}")
    if fresh:
        fresh_restart(task.id, cfg, log=log)
    if continue_:
        resume_in_flight(pipeline.lifecycle, pipeline, log=log)
    pipeline.process(task)
    return 0


def cmd_run(continue_: bool = False, requeue_stale: bool = False) -> int:
    """Process pending tasks one claim at a time, then enter autonomous mode.

    That is the difference from `run-task-loop`, which exits once pending/ is
    empty: this one ends in task generation.

    Claims are taken a single file at a time (`limit=1`), so a run can never
    hold the whole queue; anything it did not work stays in pending/. Claims
    this invocation made are handed back to pending/ on the way out, so a
    park, an exception or an early return cannot strand them. Every claim is
    taken under one owner id generated for this invocation, and the same id is
    named by the cleanup, so the hand-back can only ever move this run's own
    claims. Claims that were already sitting in claimed/ when the run started
    are left untouched unless the stale-claim guard was switched on
    (`requeue_stale` / `autoRequeueStaleClaims`), and even then the guard is
    scoped to this invocation's id, so it cannot move a peer's claim or an
    unattributable one — see `_requeue_stale_claims` for why it is off.
    """
    cfg, store, runner, provider, pipeline, log = build()
    owner = _new_owner_id("run")
    _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                          enabled=_requeue_stale_enabled(cfg, requeue_stale),
                          log=log, owner=owner)
    claimed: list[Task] = []
    try:
        if continue_:
            resume_in_flight(pipeline.lifecycle, pipeline, log=log)
        while True:
            if not provider.count_pending():
                log("pending queue empty")
                break
            tasks = provider.fetch_pending(claim=True, limit=1, owner=owner)
            task = tasks[0]
            claimed.extend(tasks)
            log(f"processing {task.id}")
            try:
                pipeline.process(task)
            except Exception as e:
                # one bad task must not strand the rest of the queue
                log(f"  task {task.id} raised {type(e).__name__}: {e}; skipping")
                continue
        if not provider.fetch_pending():
            log("queue empty -> entering autonomous mode")
            gen = AutonomousGenerator(cfg, runner, provider, log=log)
            # generate against the harness's own repo (self-improvement)
            gen.run(Path(__file__).resolve().parent)
        return 0
    finally:
        _release_run_claims(provider, claimed, owner, log)


def _release_run_claims(provider, claimed: list[Task], owner: str, log) -> int:
    """Hand back this run's own claims, and log how many.

    Only the tasks named in `claimed` are moved, and only those the named
    `owner` still holds: pre-existing entries in claimed/ belong to the human
    review pass, not to this run (see `provider.requeue_all_claims()`, which is
    the operator command's job), and a claim held by another live invocation is
    refused by the provider rather than stolen. A claim already consumed by the
    pipeline is simply not there to move.
    """
    released = 0
    for task in claimed:
        if provider.requeue_claim(task, owner=owner):
            released += 1
            log(f"  released claim: {task.id}")
    if released:
        log(f"released {released} unprocessed claim(s) back to pending")
    return released


def cmd_run_one() -> int:
    """Claim and process at most one pending task, then exit.

    Anything the claim fetch returned but this call did not process is handed
    back to pending/ for a later cycle. The claims are taken and handed back
    under one owner id generated for this invocation, so the hand-back cannot
    move a claim another invocation is holding. Not used by the supervisor.
    """
    cfg, store, runner, provider, pipeline, log = build()
    owner = _new_owner_id("run-one")
    if not provider.count_pending():
        log("no pending tasks to claim")
        return 0
    tasks = provider.fetch_pending(claim=True, owner=owner)
    if not tasks:
        log("no pending tasks to claim")
        return 0
    task = tasks[0]
    log(f"processing {task.id} ({len(tasks)} claimed this cycle)")
    pipeline.process(task)
    # Drop the claim we just processed so it does not sit in claimed/ while
    # the extras are handed back. The real pipeline's `process` does this via
    # `release_claim`; the test stub does not, so we do it here.
    provider.release_claim(task)
    # release any other claims made this cycle that we did not process,
    # returning them to pending so a future cycle picks them up.
    for other in tasks[1:]:
        if provider.requeue_claim(other, owner=owner):
            log(f"  requeued unprocessed claim: {other.id}")
    return 0


def cmd_run_task_loop(continue_: bool = False, requeue_stale: bool = False) -> int:
    """Process pending tasks one at a time until the queue is empty.

    With `--continue`, every `active/` task that has a `task.json` is resumed
    before `pending/` is touched. Tasks are claimed one at a time, and the
    loop returns once `pending/` is empty. This is the subcommand T14's pure
    `command_for_action` maps RESUME and WORK to; T38 checks that mapping and
    the supervisor call site. Every claim this loop takes is recorded against
    one owner id generated for this invocation, and the hand-back of the claims
    it did not process names that same id, so another run's claim is never
    moved here. The stale-claim guard is scoped to that same id, so it reclaims
    only this invocation's own aged claims and leaves a peer's — and any claim
    nobody can be shown to hold — where they are: `_requeue_stale_claims`.
    """
    cfg, store, runner, provider, pipeline, log = build()
    owner = _new_owner_id("run-task-loop")
    _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                          enabled=_requeue_stale_enabled(cfg, requeue_stale),
                          log=log, owner=owner)
    if continue_:
        resume_in_flight(pipeline.lifecycle, pipeline, log=log)
    while True:
        tasks = provider.fetch_pending(claim=True, owner=owner)
        if not tasks:
            log("pending queue empty")
            return 0
        task = tasks[0]
        log(f"processing {task.id} ({len(tasks)} pending)")
        pipeline.process(task)
        for other in tasks[1:]:
            if provider.requeue_claim(other, owner=owner):
                log(f"  requeued unprocessed claim: {other.id}")


@dataclass(frozen=True)
class StaleClaim:
    """One claim selected for requeue, with the age and the owner it was selected on.

    The age and the recorded owner are read once, at selection, so a report line
    shows what the decision was made on rather than whatever the clock or the
    sidecar says after the move. `owner` is `OWNER_UNKNOWN` for a claim with no
    readable sidecar — the state that only an explicit operator force may move.
    """
    claim: Task
    age_hours: float
    owner: str = OWNER_UNKNOWN

    @property
    def label(self) -> str:
        return f"{self.claim.id} ({int(self.age_hours)}h)"


def _silent(line: str = "") -> None:
    """No-op log sink, for calling the guard directly (tests, ad-hoc use)."""


def _stale_claims(provider, older_hours: float) -> list[StaleClaim]:
    """The provider's claims aged `older_hours` or more, with who holds each.

    A claim the provider cannot age (-1.0) is skipped, never read as old.
    Ownership comes from `list_owned_claims()` rather than the plain task view,
    so every selection carries the recorded owner both reclaim policies read.
    """
    stale = []
    for claim in provider.list_owned_claims():
        age = provider.claim_age_hours(claim.task.id)
        if age >= 0 and age >= older_hours:
            stale.append(StaleClaim(claim=claim.task, age_hours=age,
                                    owner=claim.owner))
    return stale


def _requeue_stale_enabled(cfg, requeue_stale: bool) -> bool:
    """The automatic guard runs when the operator said so on the CLI or in
    config.json (`autoRequeueStaleClaims`). Absent or false in both = off.

    The config read is defensive on purpose: a caller that hands in a cfg
    without a `get()` (a partial wiring, a stub) simply has no opinion on the
    flag, and reading an optional setting must never be what kills a run path
    before it has claimed anything.
    """
    configured = cfg.get("autoRequeueStaleClaims", False) if hasattr(cfg, "get") else False
    return bool(requeue_stale) or bool(configured)


def _requeue_stale_claims(provider, older_hours: float, enabled: bool,
                          log=None, owner: str | None = None) -> int:
    """Hand every claim aged `older_hours`+ back to pending/. Off unless enabled.

    Deliberately opt-in (decision D4): the entries sitting in claimed/ are the
    input to the human review pass, and an always-on guard would empty the
    directory on the first loop. A claim younger than `older_hours` is left
    alone either way — that is a concurrent run's, not garbage.

    Naming an `owner` makes the sweep ownership-aware, which is how the run
    commands call it: a stale claim is reclaimed only when its sidecar names
    that same owner. A stale claim held by another invocation is left for its
    holder, and a claim with no readable owner — an absent or a corrupt sidecar,
    both of which read `OWNER_UNKNOWN` — is left for an operator, because age
    alone cannot tell a dead run's orphan from a live one's work. Each skip is
    logged with the recorded owner so there is something to act on.

    With no `owner` the sweep is the pre-ownership call and checks nothing: that
    is an operator's authority, not a run's. Returns the number of claims moved
    (0 when disabled).
    """
    if log is None:
        log = _silent
    if not enabled:
        return 0
    moved = 0
    for item in _stale_claims(provider, older_hours):
        if owner is not None and item.owner != owner:
            if item.owner == OWNER_UNKNOWN:
                log(f"  ⚠ not reclaiming {item.label}: no readable owner, so "
                    f"nothing here can prove it is abandoned; an explicit "
                    f"operator requeue is required")
            else:
                log(f"  ⚠ not reclaiming {item.label}: held by {item.owner}, "
                    f"not {owner}")
            continue
        if provider.requeue_claim(item.claim, owner=owner) is not None:
            moved += 1
            log(f"  reclaimed stale claim: {item.label}")
    if moved:
        log(f"requeued {moved} stale claim(s) (>= {older_hours:g}h)")
    return moved


def cmd_requeue_claims(older_than: float = 0.0, dry_run: bool = False,
                       force: bool = False) -> int:
    """Recover stranded claims: move claimed-but-unprocessed files back to pending/.

    `--older-than` bounds the sweep (default 0.0 = every claim); `--dry-run`
    prints the plan and touches nothing. An empty claimed/ is the healthy
    case, not an error, so this returns 0 either way.

    Every requeue names the claim's recorded owner, so each line says whose
    claim is being handed back — this command acts on an operator's authority,
    not on an owner id of its own. A claim with no readable owner names nobody,
    and it is refused until the command is forced: an unattributable claim is
    evidence about some invocation, and only an explicit operator decision may
    move it. `force` is that decision, and it reaches no further than
    `older_than` — a young claim is somebody's live work whatever the flags.

    The override is this command's alone: `provider.requeue_claim(force=True)`
    is documented for it, and no run path passes it. It is a handler parameter
    for now — the CLI flag is parser work this leaf does not own.
    """
    _, _, _, provider, _, log = build()
    total = len(provider.list_claims())
    stale = _stale_claims(provider, older_than)
    moved = refused = 0
    for item in stale:
        if not force and item.owner == OWNER_UNKNOWN:
            refused += 1
            log(f"  ⚠ not requeueing {item.label}: owner is unknown, so an "
                f"explicit force is required to hand it back")
            continue
        if dry_run:
            log(f"would requeue {item.label} owner={item.owner}")
            continue
        result = provider.requeue_claim(item.claim, owner=item.owner, force=force)
        if result is not None:
            moved += 1
            log(f"requeued {item.label} owner={item.owner}")
    if dry_run:
        log(f"dry run: {len(stale) - refused} of {total} claim(s) at or over "
            f"{older_than:g}h would move to pending/")
        return 0
    log(f"requeued {moved} of {len(stale)}")
    return 0


def cmd_autonomous() -> int:
    cfg, store, runner, provider, pipeline, log = build()
    gen = AutonomousGenerator(cfg, runner, provider, log=log)
    gen.run(Path(__file__).resolve().parent)
    return 0


def _queue_names(queue_dir: Path, sub: str) -> list[str]:
    """Entry names in one queue subdirectory, sorted; [] when it is missing."""
    d = queue_dir / sub
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _claim_labels(provider) -> list[str]:
    """One `<id> (<age>h)` label per claim the provider is holding.

    Ages are whole hours from `provider.claim_age_hours()`; a claim the
    provider cannot age (it reports -1.0) is listed without an age rather than
    with a bogus one.
    """
    labels = []
    for claim in provider.list_claims():
        age = provider.claim_age_hours(claim.id)
        labels.append(f"{claim.id} ({int(age)}h)" if age >= 0 else claim.id)
    return labels


def cmd_status() -> int:
    cfg, store, runner, provider, pipeline, log = build()
    claims = _claim_labels(provider)
    for sub in QUEUE_LOCATIONS_ALL:
        items = claims if sub == CLAIMED_LOCATION else _queue_names(cfg.queue_dir, sub)
        log(f"{sub:<10} ({len(items)}): {', '.join(items) if items else '-'}")
    if claims:
        log(CLAIMS_STRANDED_WARNING.format(count=len(claims)))
    log()
    log(render_report(store.all()))
    return 0


# The review location holds each terminal task's summary file, the only place
# a park/fail reason is recorded. No enum member covers it (TaskStatus is the
# task's own state, not a file location), so the directory name lives here.
_REVIEW_LOCATION = "review"


def _board_task_id(queue_dir: Path, sub: str, name: str) -> str:
    """The task id of one queue entry: file entries lose their `.md` suffix."""
    path = queue_dir / sub / name
    return path.stem if path.is_file() else name


def _stats_rows_by_task(rows: list[dict]) -> dict[str, list[dict]]:
    """Index session rows by task id, preserving append order within a task.

    One pass over the store so the board reads `sessions.jsonl` once, not
    once per task. Rows without a task id are dropped: nothing can show them.
    """
    by_task: dict[str, list[dict]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if task_id:
            by_task.setdefault(task_id, []).append(row)
    return by_task


def _claim_owner(queue_dir: Path, claim) -> str:
    """The owner recorded in a claim's ownership sidecar.

    The provider names the claim file in `source` (`claimed:<file>`); a
    provider that does not, or a sidecar that is absent or corrupt, reads
    back as `OWNER_UNKNOWN` (the renderer shows `?`, spec FR-3).
    """
    prefix = f"{CLAIMED_LOCATION}:"
    if not claim.source.startswith(prefix):
        return OWNER_UNKNOWN
    claim_file = queue_dir / CLAIMED_LOCATION / claim.source[len(prefix):]
    return read_metadata(claim_file).owner


def _terminal_reason(queue_dir: Path, task_id: str) -> str:
    """The park/fail reason of a terminal task, best-effort (spec FR-3).

    `TaskLifecycle.park`/`fail` record the reason as the executive summary
    of `review/<id>.md`; neither `task.json` nor the stats rows carry one.
    No file, unreadable file, or no summary section all read as no reason —
    absence is not an error.
    """
    try:
        text = (queue_dir / _REVIEW_LOCATION / f"{task_id}.md").read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    heading = "## Executive summary"
    if heading not in lines:
        return ""
    for line in lines[lines.index(heading) + 1:]:
        stripped = line.strip()
        if stripped:
            return "" if stripped.startswith("#") else stripped
    return ""


def _board_task(queue_dir: Path, sub: str, name: str, *,
                owner: str = "", reason: str = "",
                stats_rows: list[dict] | None = None) -> BoardTask:
    """Collect one queue entry for the board: id, origin, timestamps, state.

    The task id is the entry name, minus a `.md` suffix for the file-shaped
    locations (`pending/`, `review/`, `claimed/`). `task.json` is read as plain
    JSON rather than through `TaskLifecycle` because the board must report a
    corrupt file as `state: unknown` instead of the tolerant defaults
    `load_state()` produces, and because that loader would log its warnings to
    a query command's stdout. Missing, unreadable, unparseable or non-object
    all read the same way: no timestamp, no state (spec FR-7).

    Read-only (FR-8): one directory listing, plain reads, no stat-write, no
    claim. `owner`, `reason` and `stats_rows` are collected by `cmd_board`
    (they come from the claim sidecar, the review summary and the stats
    store respectively) and ride along untouched.
    """
    path = queue_dir / sub / name
    task_id = _board_task_id(queue_dir, sub, name)
    last_updated = ""
    state_readable = False
    stage = ""
    checkpointed_stages: tuple[str, ...] = ()
    try:
        raw = json.loads((path / "task.json").read_text())
        if isinstance(raw, dict):
            state_readable = True
            last_updated = str(raw.get("last_updated") or "")
            stage = str(raw.get("stage") or "")
            checkpoints = raw.get("checkpointed_stages")
            if isinstance(checkpoints, list):
                checkpointed_stages = tuple(str(s) for s in checkpoints)
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return BoardTask(task_id=task_id, origin=classify_origin(task_id),
                     last_updated=last_updated, mtime=mtime,
                     state_readable=state_readable, stage=stage,
                     checkpointed_stages=checkpointed_stages, owner=owner,
                     reason=reason,
                     stats=collapse_task_stats(stats_rows or []))


def cmd_board() -> int:
    """Print the kanban-style board: executive summary over the location sections.

    Read-only (spec FR-8): counts come from directory listings and
    `provider.list_claims()` — nothing is claimed, moved or written. The
    claimed count is the claim list, not the directory listing, so ownership
    sidecars never count as tasks (consistent with `cmd_status`). Rendering is
    the pure `core.board.render_board`; this handler only collects.
    """
    cfg, store, _, provider, _, _ = build()
    claims = provider.list_claims()
    rows_by_task = _stats_rows_by_task(store.all())
    boards = []
    for sub in QUEUE_LOCATIONS_ALL:
        if sub == CLAIMED_LOCATION:
            owners = {claim.id: _claim_owner(cfg.queue_dir, claim)
                      for claim in claims}
            names = [claim.id for claim in claims]
        else:
            owners = {}
            names = _queue_names(cfg.queue_dir, sub)
        tasks = []
        for name in names:
            task_id = _board_task_id(cfg.queue_dir, sub, name)
            tasks.append(_board_task(
                cfg.queue_dir, sub, name,
                owner=owners.get(name, ""),
                reason=(_terminal_reason(cfg.queue_dir, task_id)
                        if sub in TERMINAL_LOCATIONS else ""),
                stats_rows=rows_by_task.get(task_id, [])))
        boards.append(LocationBoard(location=sub, tasks=tuple(tasks)))
    warning = CLAIMS_STRANDED_WARNING.format(count=len(claims)) if claims else None
    print(render_board(BoardSummary(locations=tuple(boards),
                                     claims_warning=warning,
                                     stats=aggregate_stats(store.all()))))
    return 0


def cmd_report() -> int:
    _, store, *_ = build()
    print(render_report(store.all()))
    return 0


def cmd_journey(task_id: str | None = None, save: bool = False) -> int:
    """Print the static workflow journey graph and diagnostics for a task."""
    cfg, store, *_ = build()
    all_rows = store.all()
    if not all_rows:
        print("No sessions recorded yet in stats.")
        return 0

    if not task_id:
        # Find the most recently recorded task_id
        for r in reversed(all_rows):
            tid = r.get("task_id")
            if tid and tid != "None":
                task_id = tid
                break

    if not task_id:
        print("No specific task found in stats. Showing journey for all recorded sessions:")
        print(render_task_journey(all_rows, task_id="all"))
        return 0

    rows = store.for_task(task_id)
    if not rows:
        print(f"No sessions found for task '{task_id}'.")
        return 1

    text = render_task_journey(rows, task_id=task_id)
    print(text)

    if save:
        path = store.write_task_journey(task_id)
        print(f"\n[Saved journey graph to {path}]")

    return 0


def cmd_resume(task_id: str, yes: bool = False, fresh: bool = False) -> int:
    """Resume a task from its last checkpoint (spec FR3)."""
    cfg, store, runner, provider, pipeline, log = build()
    return resume_task(task_id, yes, cfg, pipeline,
                       lifecycle=pipeline.lifecycle, log=log, fresh=fresh)


def cmd_unpark(task_id: str) -> int:
    """Move a parked (or failed) task back to pending so it is re-processed.

    The task's artifacts (spec, slices, progress) are preserved, so the next
    run continues from where it got to rather than starting over.
    """
    cfg, _, _, _, _, log = build()
    moved = False
    for src_folder in ("parked", "failed"):
        src = cfg.queue_dir / src_folder / task_id
        if src.exists():
            dst = cfg.queue_dir / "pending" / f"{task_id}.md"
            original = src / "original.md"
            if original.exists():
                dst.write_text(original.read_text())
            else:
                dst.write_text(f"# {task_id}\n\n(requeued from {src_folder}; original requirement missing)\n")
            # remove the old terminal dir so it starts fresh in active/
            shutil.rmtree(src)
            # drop any stale exec summary
            (cfg.queue_dir / "review" / f"{task_id}.md").unlink(missing_ok=True)
            log(f"unparked {task_id}: {src_folder} -> pending/{task_id}.md")
            moved = True
    if not moved:
        log(f"{task_id} not found in parked/ or failed/")
        return 1
    return 0