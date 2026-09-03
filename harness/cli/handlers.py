"""Command handlers for the harness CLI."""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..workflow.autonomous import AutonomousGenerator
from ..workflow.continue_fresh import fresh_restart, resume_in_flight
from ..workflow.resume import resume_task
from ..workflow.task_lifecycle import CLAIMED_LOCATION, QUEUE_LOCATIONS_ALL
from ..core.board import (TERMINAL_LOCATIONS, BoardSummary, BoardTask,
                          LocationBoard, RenderContext, aggregate_stats,
                          classify_origin, collapse_task_stats, render_board,
                          write_board)
from ..core import task_record
from ..core.claim_metadata import OWNER_UNKNOWN
from ..core.interrupt import (
    InterruptMode,
    InterruptState,
    clear_interrupt,
    interrupt_age_seconds,
    read_interrupt,
    write_interrupt,
)
from ..core.process_lock import LockHeldError, ProcessLock, RUN_LOCK_NAME
from ..core.providers import Task
from ..core.stand_down import StandDownWatcher
from ..core.syncd import SyncdLoop, SyncdParams
from external.harness_cli import spawn_harness_run_task_loop
from ..core.stats import render_report, render_task_journey, render_task_journey_markdown, render_report_json
from external.pi_cli import run_quick_pi_session

from ..composition import build, build_github_api, build_sync_engine

# Shown under the status table while claims exist. Names the command that
# clears them (it exists now — T12), so the line is something to type.
CLAIMS_STRANDED_WARNING = ("⚠ {count} claimed tasks: nothing will process them "
                           "until they are requeued (harness.py requeue-claims).")

# A claim older than this is stranded, not in-flight. Module constant, so both
# entrypoints and the operator command agree on one number; env-overridable for
# a box whose sessions legitimately run longer.
CLAIM_STALE_HOURS = float(os.environ.get("CLAIM_STALE_HOURS", "6.0"))

# CSV export command handler
def cmd_export_stats_csv(csv_path: str) -> int:
    """Export raw session stats to a CSV file.

    Reads the stats store (default path from config) and writes a CSV with
    columns matching the SessionRecord fields. Returns 0 on success, 1 on error.
    """
    # Build config to locate stats store
    cfg, store, *_ = build()
    rows = store.all()
    if not rows:
        print("No stats to export.")
        return 0
    # Determine output path
    out_path = Path(csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Write CSV
    import csv
    fieldnames = ["ts", "task_id", "stage", "model", "verdict", "outcome", "peak_tokens",
                  "duration_s", "rc", "prompt_chars", "slice", "iteration", "session_file", "notes"]
    with out_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # Ensure all keys exist
            row = {k: r.get(k, "") for k in fieldnames}
            writer.writerow(row)
    print(f"Exported {len(rows)} session records to {out_path}")
    return 0

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


def _interrupt_takes_work_away(cfg, log) -> bool:
    """Top guard for single-session commands: True when an interrupt is active.

    `run`, `run-one`, `run-task` and `resume <task_id>` have no in-run
    boundary, so honoring the file (FR-5.4, FR-3.1) means taking no work at
    all: the caller returns 0 without claiming or spawning `pi` against the
    model the operator reclaimed. The file is only *read*, never cleared or
    transitioned — only no-arg `resume` or quick-mode completion may do that.
    A corrupt file reads fail-safe as an active stand-down (FR-5.3).

    A cfg without a `work_dir` (a partial wiring) has no state file to read,
    so it has no interrupt — same defensive read as `_requeue_stale_enabled`.
    """
    work_dir = getattr(cfg, "work_dir", None)
    if work_dir is None:
        return False
    status = read_interrupt(work_dir, log=log)
    if status is None:
        return False
    log(f"interrupt active (mode={status.mode.name} state={status.state.name}): "
        f"taking no work; `harness.py resume` clears the request")
    return True


def _stand_down_at_boundary(cfg, log) -> bool:
    """Session-boundary check for the run loops: True when the loop must stop.

    Called where the loop would otherwise take new work (FR-6.1: the safe
    boundary is immediately before spawning a new `pi` session). When an
    interrupt is active the request is acknowledged (`requested -> paused`,
    via the one owner of that file), and the caller unwinds by returning 0:
    no parking, no crash-retry, tasks stay in `active/` with their
    checkpoints and claims (FR-6.2/FR-6.4/FR-6.5).

    A cfg without a `work_dir` (a partial wiring) has no state file to read,
    so it has no interrupt — same defensive read as `_requeue_stale_enabled`.
    """
    return StandDownWatcher(getattr(cfg, "work_dir", None), log=log)()


@contextlib.contextmanager
def _run_lock(cfg, log):
    """Hold `<workDir>/run.lock` for the life of a run command (FR-4.3).

    The daemon reads this lock before spawning, so a harness run started by
    hand blocks spawning equally. Yields False — without holding anything —
    when a live process already holds the lock; the caller returns non-zero.
    A cfg without a `work_dir` (a partial wiring, same defensive read as
    `_requeue_stale_enabled`) has no lock to take and yields True: the run
    proceeds unrecorded rather than dying on an absent attribute.
    """
    work_dir = getattr(cfg, "work_dir", None)
    if work_dir is None:
        yield True
        return
    lock = ProcessLock(Path(work_dir), RUN_LOCK_NAME)
    try:
        lock.acquire()
    except LockHeldError as exc:
        log(f"harness run refused: {exc}")
        yield False
        return
    try:
        yield True
    finally:
        lock.release()


def cmd_run_task(file: str, fresh: bool = False, continue_: bool = False,
                 repo: str | Path | None = None) -> int:
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _interrupt_takes_work_away(cfg, log):
        return 0
    with _run_lock(cfg, log) as acquired:
        if not acquired:
            # FR-4.3: a hand-started single-task run is a harness run — it
            # holds `run.lock` for its life so the daemon (and a peer
            # command) sees it and never starts a second concurrent
            # pipeline against the model.
            return 1
        if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
            runner.validate_models()
        task = Task(id=_slug(Path(file).stem),
                    body=Path(file).read_text(), source=f"cli:{file}")
        if fresh:
            fresh_restart(task.id, cfg, log=log)
        if continue_:
            resume_in_flight(pipeline.lifecycle, pipeline, log=log)
        pipeline.process(task)
    return 0


def cmd_run(continue_: bool = False, requeue_stale: bool = False,
            repo: str | Path | None = None) -> int:
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
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _interrupt_takes_work_away(cfg, log):
        return 0
    with _run_lock(cfg, log) as acquired:
        if not acquired:
            # Another harness holds the run lock (FR-4.3): refuse rather
            # than run a second concurrent pipeline against the model.
            return 1
        if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
            runner.validate_models()
        owner = _new_owner_id("run")
        _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                              enabled=_requeue_stale_enabled(cfg, requeue_stale),
                              log=log, owner=owner)
        claimed: list[Task] = []
        try:
            if continue_:
                resume_in_flight(pipeline.lifecycle, pipeline, log=log)
            while True:
                if _stand_down_at_boundary(cfg, log):
                    # The boundary before this cycle's claim/spawn (FR-6.1):
                    # acknowledge, stop taking work, unwind through the
                    # finally-block claim hand-back — no parking, no
                    # crash-retry, exit 0 (FR-6.2/FR-6.4).
                    return 0
                if not provider.count_pending():
                    log("pending queue empty")
                    break
                tasks = provider.fetch_pending(claim=True, limit=1,
                                               owner=owner)
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
                gen = AutonomousGenerator(cfg, runner, provider, log=log,
                                          sync_engine=getattr(pipeline, "sync_engine", None))
                target_repo = getattr(cfg, "repo_dir", None) or Path(__file__).resolve().parent.parent
                gen.run(target_repo,
                        stand_down_check=lambda: _stand_down_at_boundary(cfg, log))
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


def cmd_run_one(repo: str | Path | None = None) -> int:
    """Claim and process at most one pending task, then exit.

    Anything the claim fetch returned but this call did not process is handed
    back to pending/ for a later cycle. The claims are taken and handed back
    under one owner id generated for this invocation, so the hand-back cannot
    move a claim another invocation is holding. Not used by the supervisor.
    """
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _interrupt_takes_work_away(cfg, log):
        return 0
    if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
        runner.validate_models()
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


def cmd_run_task_loop(continue_: bool = False, requeue_stale: bool = False,
                      repo: str | Path | None = None) -> int:
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
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _stand_down_at_boundary(cfg, log):
        # An interrupt was already active when the loop started: acknowledge
        # it and unwind before claiming anything (FR-5.4, FR-6.2).
        return 0
    with _run_lock(cfg, log) as acquired:
        if not acquired:
            # Another harness holds the run lock (FR-4.3): refuse rather
            # than run a second concurrent pipeline against the model.
            return 1
        if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
            runner.validate_models()
        owner = _new_owner_id("run-task-loop")
        _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                              enabled=_requeue_stale_enabled(cfg, requeue_stale),
                              log=log, owner=owner)
        if continue_:
            resume_in_flight(pipeline.lifecycle, pipeline, log=log)
        while True:
            if _stand_down_at_boundary(cfg, log):
                # The boundary between tasks, and the check immediately before
                # this cycle's fetch/claim/spawn: acknowledge, stop taking
                # work, unwind with the checkpoints and claims where they are
                # (FR-6.1, FR-6.2) — no parking, no crash-retry, exit 0.
                return 0
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

    The command is also the queue's metadata hygiene pass (FR-E5): it first
    migrates every legacy sidecar into the task records — including orphans
    whose markdown is gone, which no task read ever sights — then reports
    and cleans claim records whose task exists nowhere (§5.8), so an orphan
    claim record cannot accumulate unseen. Running the command twice
    converges: the second pass finds nothing to migrate and nothing to
    clean (FR-E4). `dry_run` keeps the queue byte-identical: both the sweep
    and the orphan pass report their plan and write nothing.
    """
    _, _, _, provider, _, log = build()
    migrated = provider.sweep_legacy_metadata(dry_run=dry_run)
    if migrated:
        verb = "would migrate" if dry_run else "migrated"
        log(f"{verb} legacy metadata for {len(migrated)} task(s): "
            f"{', '.join(migrated)}")
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
    orphans_reported = _clean_orphan_claims(provider, older_than, dry_run, log)
    if dry_run:
        log(f"dry run: {len(stale) - refused} of {total} claim(s) at or over "
            f"{older_than:g}h would move to pending/, "
            f"{orphans_reported} orphan claim record(s) would be cleaned")
        return 0
    log(f"requeued {moved} of {len(stale)}")
    return 0


def _clean_orphan_claims(provider, older_than: float, dry_run: bool,
                         log) -> int:
    """Report and clean claim records whose task markdown is gone (§5.8).

    A claim record with no task anywhere is not a claim on work — no
    fetch, board or claim listing can see it (FR-A4) — but it is metadata
    an operator must be able to see and remove: the `002-…` defect class,
    an orphan that outlived its markdown. Age is measured from the
    record's own `claimed_at`, the only clock an orphan has; a corrupt
    timestamp reads 0.0 and so always ages in.

    Cleaning drops the `claim` section (and the record itself when nothing
    else is left); a `github` section survives — linkage outlives the
    markdown by design (§5.4). A failed clean is logged, never fatal
    (FR-D3). Returns the number reported (cleaned, or planned under
    `dry_run`) so the caller can count them in its summary.
    """
    now = time.time()
    count = 0
    for orphan in provider.list_orphan_claims():
        age_hours = (now - orphan.claimed_at) / 3600.0
        if age_hours < older_than:
            continue
        label = f"{orphan.task_id} ({int(age_hours)}h)"
        count += 1
        if dry_run:
            log(f"would clean orphan claim record {label} "
                f"owner={orphan.owner}")
            continue
        log(f"orphan claim record {label} owner={orphan.owner}: "
            f"no task markdown in any queue location")
        if provider.clean_orphan_claim(orphan):
            log(f"cleaned orphan claim record {label}")
    return count


def cmd_autonomous(repo: str | Path | None = None) -> int:
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _stand_down_at_boundary(cfg, log):
        return 0
    if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
        runner.validate_models()
    gen = AutonomousGenerator(cfg, runner, provider, log=log,
                              sync_engine=getattr(pipeline, "sync_engine", None))
    target_repo = getattr(cfg, "repo_dir", None) or Path(__file__).resolve().parent.parent
    # The generator owns the per-attempt boundary: it is what spawns the
    # suggest/review `pi` sessions, so the stand-down check travels with the
    # loop rather than only guarding its single call site.
    gen.run(target_repo, stand_down_check=lambda: _stand_down_at_boundary(cfg, log))
    return 0


def _queue_names(queue_dir: Path, sub: str) -> list[str]:
    """Task entry names in one queue subdirectory, sorted; [] when missing.

    A legacy metadata sidecar still sitting in a queue location is not a
    task (FR-A4) and is skipped here, so status and the board never
    render one as work. The record store is a dot-directory outside every
    queue location and cannot appear at all.
    """
    d = queue_dir / sub
    return sorted(p.name for p in d.iterdir()
                  if not task_record.is_legacy_metadata_name(p.name)) \
        if d.exists() else []


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


def _format_interrupt_age(seconds: float) -> str:
    """One interrupt age as a short operator-readable duration (`42s`, `5m03s`, `2h05m`)."""
    whole = int(max(seconds, 0.0))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _interrupt_status_line(cfg, log) -> None:
    """Print the interrupt line (spec FR-4.1) when one is active; nothing when not.

    Shared by `status` and `board`: mode and state render as their Enum
    names (STAND_DOWN/QUICK, REQUESTED/PAUSED), the age counts from
    `requested_at`. A corrupt file reads fail-safe as STAND_DOWN/REQUESTED
    through `read_interrupt`, which sends its recovery warning to `log`, so
    the surface shows the fail-safe record plus the hint (spec FR-5.3).
    """
    status = read_interrupt(cfg.work_dir, log=log)
    if status is None:
        return
    age = _format_interrupt_age(interrupt_age_seconds(status))
    log(f"interrupt: mode={status.mode.name} state={status.state.name} "
        f"age={age}")


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
    _interrupt_status_line(cfg, log)
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
    """The owner recorded in the task's metadata record.

    The provider names the claim file in `source` (`claimed:<file>`); a
    provider that does not, or a task whose record holds no readable
    `claim` section, reads back as `OWNER_UNKNOWN` (the renderer shows `?`,
    spec FR-3). Resolution is by task id through the record API (FR-B1).
    """
    prefix = f"{CLAIMED_LOCATION}:"
    if not claim.source.startswith(prefix):
        return OWNER_UNKNOWN
    task_id = Path(claim.source[len(prefix):]).stem
    held = task_record.read_record(queue_dir, task_id).claim
    return held.owner if held is not None else OWNER_UNKNOWN


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


# Terminal width the board assumes when the stream reports none (spec FR-6).
_BOARD_FALLBACK_WIDTH = 80


def _board_context() -> RenderContext:
    """The terminal facts for the board renderer (spec FR-6).

    Color only when stdout is a TTY and `NO_COLOR` is unset; width from
    `shutil.get_terminal_size` (which also honors a `COLUMNS` override) with
    an 80-cell fallback. The renderer takes these as data and never touches
    the environment or the stream itself.
    """
    width = shutil.get_terminal_size(
        fallback=(_BOARD_FALLBACK_WIDTH, 24)).columns
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    return RenderContext(use_color=use_color, width=width)


def cmd_board(json: bool = False) -> int:
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
    board_summary = BoardSummary(locations=tuple(boards),
                                      claims_warning=warning,
                                      stats=aggregate_stats(store.all()))
    if json:
        # Output JSON representation of the board summary
        from dataclasses import asdict
        import json as _json
        data = asdict(board_summary)
        # Ensure any enum values are serialized as their value (they are already strings in summary)
        print(_json.dumps(data, indent=2))
    else:
        board = render_board(board_summary, _board_context())
        write_board(board, sys.stdout)
    # The board's output stream is stdout, so the interrupt line (and a
    # corrupt-file warning) travels there too, not through the logger.
    _interrupt_status_line(cfg, lambda line: print(line))
    return 0


def cmd_report_json() -> int:
    """Print the stats report as JSON for external tooling."""
    _, store, *_ = build()
    import json as _json
    data = render_report_json(store.all())
    print(_json.dumps(data, indent=2))
    return 0

def cmd_report() -> int:
    # Existing command unchanged
    _, store, *_ = build()
    print(render_report(store.all()))
    return 0


def cmd_stats_prune(max_rows: int | None = None) -> int:
    """Trim the stats JSONL file.

    ``max_rows`` overrides the config default. If omitted, falls back to
    ``cfg.maxStatsRows`` if present, otherwise 10000.
    """
    cfg, store, *_ = build()
    default = getattr(cfg, "maxStatsRows", 10000)
    keep = max_rows if max_rows is not None else default
    try:
        store.prune(keep)
    except Exception as exc:
        print(f"Failed to prune stats: {exc}")
        return 1
    print(f"Stats pruned to most recent {keep} rows.")
    return 0


# Operator-visible contract (spec FR-3, AC-1): this exact line, exit 0, when
# GitHub is unconfigured. Keep verbatim.
GITHUB_SYNC_DISABLED = "github sync disabled"


def cmd_sync() -> int:
    """Run one manual two-way GitHub sync pass (spec FR-3, manual).

    Disabled-safe (FR-0.1): with `githubPat` or `githubRepo` empty or absent
    the whole feature is inert — the disabled line is printed, nothing else
    happens, and the command exits 0.

    With GitHub configured one full two-way pass runs and its summary line
    is logged (FR-3, manual). A rate-limit/auth abort is a reported pass,
    not an error: the summary carries `ABORTED` and the exit is 0 — the
    unfinished work rolls to the next pass (spec edge 9). Any other
    failure is reported, never a crash (NFR-1): the command exits
    non-zero with the (PAT-scrubbed) error.
    """
    cfg, _store, _runner, _provider, _pipeline, log = build()
    if not cfg.github_sync_enabled:
        log(GITHUB_SYNC_DISABLED)
        return 0
    api = build_github_api(cfg, log=log)
    engine = build_sync_engine(cfg, log=log, api=api)
    try:
        # A manual pass is the no-task-id dispatch: one full two-way pass
        # (spec FR-3, manual).
        report = engine.on_stage_change()
    except Exception as exc:
        log(f"github sync pass failed: {exc}")
        return 1
    log(report.summary_line())
    return 0


def _register_syncd_signals(loop, log) -> None:
    """SIGINT/SIGTERM stop the daemon after its current pass (FR-4.6).

    The handler only flips the loop's stop flag and logs; the loop then
    finishes the pass it is in, removes `syncd.lock`, and `cmd_syncd`
    exits 0. Handlers are registered for the daemon's life only — this
    command is the daemon, so nothing restores them.

    The `log()` call inside the handler is not strictly async-signal-safe
    (review note, slice 11): it is accepted because the harness log sink
    is a buffered line write and CPython runs handlers between bytecodes,
    not inside a lock. The behavioural part — `request_stop()` — is
    flag-only and signal-safe on its own.
    """
    def _handler(signum, frame) -> None:  # noqa: ARG001 - signal signature
        log(f"syncd: signal {signum} received; finishing the current pass")
        loop.request_stop()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def cmd_syncd() -> int:
    """Run the sync daemon (spec FR-4, AC-9/AC-10/AC-11).

    The daemon owns `syncd.lock` (a second invocation exits non-zero with
    the lock message), and per interval runs a full sync pass plus the
    spawn check. With GitHub unconfigured the sync callable is None — the
    pass skips the sync entirely (zero HTTP, FR-0.1/NFR-2) and the daemon
    is a local `pending/` watcher only. Spawning goes through the same
    entry point as `harness run-task-loop` (FR-4.2b) and only happens when
    no run holds `run.lock` (FR-4.3).
    """
    cfg, _store, _runner, _provider, _pipeline, log = build()
    sync = None
    if cfg.github_sync_enabled:
        api = build_github_api(cfg, log=log)
        engine = build_sync_engine(cfg, log=log, api=api)
        # The no-task-id dispatch is one full two-way pass per poll
        # (FR-4.2a).
        sync = engine.on_stage_change
    loop = SyncdLoop(SyncdParams(
        work_dir=cfg.work_dir,
        sync_interval_s=cfg.github_sync_interval_s,
        sync=sync,
        spawn=spawn_harness_run_task_loop,
        log=log))
    _register_syncd_signals(loop, log)
    return loop.run()


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


def cmd_journey_md(task_id: str | None = None, save: bool = False) -> int:
    """Export the workflow journey for a task as Markdown with transcript links.

    Mirrors ``cmd_journey`` but renders a richer Markdown document suitable
    for sharing or publishing. If ``save`` is True the markdown is written to
    ``<statsDir>/journeys/<task_id>-journey.md``.
    """
    cfg, store, *_ = build()
    all_rows = store.all()
    if not all_rows:
        print("No sessions recorded yet in stats.")
        return 0

    if not task_id:
        # pick most recent task
        for r in reversed(all_rows):
            tid = r.get("task_id")
            if tid and tid != "None":
                task_id = tid
                break
    if not task_id:
        print("No specific task found in stats. Showing markdown journey for all recorded sessions:")
        rows = all_rows
        task_label = "all"
    else:
        rows = store.for_task(task_id)
        if not rows:
            print(f"No sessions found for task '{task_id}'.")
            return 1
        task_label = task_id

    # Collect transcript filenames if present
    transcript_files = [r.get("session_file") for r in rows]
    md = render_task_journey_markdown(rows, task_id=task_label, transcript_files=transcript_files)
    print(md)
    if save:
        out_dir = store.path.parent / "journeys"
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"{task_label}-journey.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"\n[Saved markdown journey to {md_path}]")
    return 0


# Default `interrupt --stand-down` wait: one session cap plus a minute of
# slack, so a session that just started still finishes inside the wait
# (spec FR-1.2). `--timeout N` overrides it.
INTERRUPT_WAIT_EXTRA_S = 60

# How often the wait polls the state file. The acknowledgement is a file
# rename, so faster polling buys nothing; 1s matches the granularity the
# supervisor's interruptible sleep already uses.
INTERRUPT_POLL_INTERVAL_S = 1.0

# Printed when the harness has acknowledged and released the model
# (spec FR-1.2). Operator-visible contract — keep verbatim.
STAND_DOWN_COMPLETE = ("harness stood down — model released "
                       "(task(s) left in active/ at checkpoints)")


class StandDownWaitResult(Enum):
    """How a wait-for-pause ended. Discrete internal state: an Enum."""
    PAUSED = "paused"
    CLEARED = "cleared"
    TIMED_OUT = "timed_out"


def wait_for_paused(work_dir: Path, timeout: float,
                    poll_interval: float = INTERRUPT_POLL_INTERVAL_S
                    ) -> StandDownWaitResult:
    """Poll the interrupt state file until `state=paused` (spec FR-1.2).

    PAUSED when a run loop acknowledged the request; CLEARED when the file
    disappeared first (a `resume` or a quick-mode completion took the request
    away, so the stand-down that was asked for will never happen); TIMED_OUT
    when `timeout` seconds elapsed with the request still pending — the
    request is left in place either way. A corrupt file reads fail-safe as
    REQUESTED, so the wait runs to its timeout.
    """
    deadline = time.monotonic() + max(float(timeout), 0.0)
    while True:
        status = read_interrupt(work_dir)
        if status is None:
            return StandDownWaitResult.CLEARED
        if status.state is InterruptState.PAUSED:
            return StandDownWaitResult.PAUSED
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return StandDownWaitResult.TIMED_OUT
        time.sleep(min(poll_interval, remaining))


def cmd_interrupt(stand_down: bool = False, no_wait: bool = False,
                  timeout: float | None = None, model: str | None = None,
                  prompt: str | None = None,
                  poll_interval: float = INTERRUPT_POLL_INTERVAL_S) -> int:
    """Request a managed stand-down of the harness (spec FR-1), or — without
    `--stand-down` — borrow the model for a quick session and auto-resume
    (spec FR-2).

    Writes the interrupt state file; the run loops honor it at their next
    session boundary. Idempotent: a second stand-down request while one is
    active changes nothing but the log and returns 0 (E1). Works with no
    harness running — the file simply records the request for the next start
    (FR-1.4). Unless `--no-wait` is given the command then polls the file
    until the harness acknowledges (FR-1.2); on timeout the request stays in
    place and the command exits non-zero with the running session's log
    pointer (FR-1.3).
    """
    if not stand_down:
        return _cmd_interrupt_quick(model=model, prompt=prompt,
                                    timeout=timeout,
                                    poll_interval=poll_interval)
    cfg, _store, _runner, _provider, _pipeline, log = build()
    existing = read_interrupt(cfg.work_dir, log=log)
    if existing is not None:
        log(f"interrupt already active (mode={existing.mode.name} "
            f"state={existing.state.name}); request unchanged")
        return 0
    write_interrupt(cfg.work_dir, InterruptMode.STAND_DOWN,
                    InterruptState.REQUESTED, requester_pid=os.getpid())
    log("interrupt requested: harness will stand down at the next "
        "session boundary")
    if no_wait:
        return 0
    wait_s = (cfg.session_timeout + INTERRUPT_WAIT_EXTRA_S
              if timeout is None else float(timeout))
    result = wait_for_paused(cfg.work_dir, wait_s,
                             poll_interval=poll_interval)
    if result is StandDownWaitResult.PAUSED:
        print(STAND_DOWN_COMPLETE)
        return 0
    if result is StandDownWaitResult.CLEARED:
        log("interrupt request was cleared before the harness paused; "
            "nothing is stood down (harness.py status shows the run state)")
        return 1
    log(f"timed out after {wait_s:.0f}s waiting for the harness to pause; "
        "the request stays in place — the harness will still stand down at "
        "its next session boundary")
    log(f"running session log: {cfg.logs_dir / 'harness.log'}")
    return 1


# Log-only transition emitted immediately before a quick-mode request file is
# deleted (spec FR-5.2): `resuming` never lives in the file, only in the log.
QUICK_RESUMING_LOG = "resuming"


def _operator_has_tty() -> bool:
    """True when a real terminal is attached for an interactive pi session."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _resolve_quick_model(cfg, requested: str | None, log) -> str | None:
    """Resolve `--model NAME` to one concrete model for a quick session.

    A valid name is a configured model (`models.*` string values or
    `modelContext` keys) or a role key whose value is a single model string.
    Pool-valued names (`fastPool`, `randomPool`) are rejected: the quick
    session pins one fixed model, not a pool (FR-2.3). Without `--model` the
    default is `models.technicalWriter`. Returns None (with the reason on
    `log`) when the name cannot be resolved — always *before* any state is
    written, so a bad name never pauses the harness (E7).
    """
    if requested is None:
        default = cfg.models.get("technicalWriter")
        if isinstance(default, str) and default.strip():
            return default.strip()
        log("interrupt: models.technicalWriter is not configured; "
            "pass --model NAME")
        return None
    value = cfg.models.get(requested)
    if isinstance(value, (list, tuple, set)):
        log(f"interrupt: '{requested}' is a model pool; the quick session "
            "pins one fixed model — pass a concrete model name instead")
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if requested in cfg.model_context_map or requested in cfg.configured_models:
        return requested
    log(f"interrupt: unknown model '{requested}'; it must be a configured "
        "model (a models.* value or a modelContext key)")
    return None


def _cancel_quick_request(work_dir: Path, log, reason: str) -> int:
    """Cancel a quick request (FR-2.5): log the reason, log `resuming`
    (log-only transition, FR-5.2), delete the file. Non-zero exit."""
    log(reason)
    log(QUICK_RESUMING_LOG)
    clear_interrupt(work_dir)
    return 1


def _cmd_interrupt_quick(model: str | None, prompt: str | None,
                         timeout: float | None,
                         poll_interval: float) -> int:
    """Quick mode (spec FR-2): stand the harness down, borrow the model for
    one `pi` session, resume automatically when that session exits.

    All validation (model resolution, already-active check, TTY check) runs
    *before* the request file is written, so a refused request never pauses
    anything. Once the harness has paused, the session runs with inherited
    stdio; on its exit (any code) the file is deleted and the harness
    resumes without a manual step. If this process is killed before that
    cleanup, the file remains and the harness stays paused (fail-safe) —
    recover with `harness.py resume` (FR-2.5, E3).
    """
    cfg, _store, _runner, _provider, _pipeline, log = build()
    resolved_model = _resolve_quick_model(cfg, model, log)
    if resolved_model is None:
        return 1
    if prompt is None and not _operator_has_tty():
        log("interrupt: no TTY attached; run through `scripts/harness-run` "
            "for an interactive quick session, or pass --prompt TEXT for a "
            "one-shot session")
        return 1
    existing = read_interrupt(cfg.work_dir, log=log)
    if existing is not None:
        log(f"interrupt: quick mode refused — an interrupt is already active "
            f"(mode={existing.mode.name} state={existing.state.name}); "
            "recover with `harness.py resume`")
        return 1
    write_interrupt(cfg.work_dir, InterruptMode.QUICK,
                    InterruptState.REQUESTED, requester_pid=os.getpid())
    log("interrupt requested (quick): harness will pause at the next "
        "session boundary")
    wait_s = (cfg.session_timeout + INTERRUPT_WAIT_EXTRA_S
              if timeout is None else float(timeout))
    result = wait_for_paused(cfg.work_dir, wait_s,
                             poll_interval=poll_interval)
    if result is StandDownWaitResult.TIMED_OUT:
        return _cancel_quick_request(
            cfg.work_dir, log,
            f"timed out after {wait_s:.0f}s waiting for the harness to "
            "pause; quick request cancelled")
    if result is StandDownWaitResult.CLEARED:
        log("interrupt: quick request was cleared before the harness paused "
            "(harness.py resume?); no session was started")
        return 1
    rc = run_quick_pi_session(model=resolved_model,
                              workdir=cfg.repo_dir or Path.cwd(),
                              prompt=prompt)
    log(f"quick session exited (rc={rc})")
    log(QUICK_RESUMING_LOG)
    clear_interrupt(cfg.work_dir)
    print("quick session finished — the harness resumes at its next boundary")
    return 0


def _resume_clear_interrupt(repo: str | Path | None = None) -> int:
    """No-arg `resume`: clear an active interrupt and let the run continue
    (spec FR-3.2). Idempotent: with no file, prints and exits 0."""
    cfg, _store, _runner, _provider, _pipeline, log = build(repo=repo)
    status = read_interrupt(cfg.work_dir, log=log)
    if status is None:
        print("no interrupt active")
        return 0
    duration = interrupt_age_seconds(status)
    log(f"interrupt cleared: mode={status.mode.name} "
        f"state={status.state.name} requested_at={status.requested_at} "
        f"duration={duration:.0f}s")
    clear_interrupt(cfg.work_dir)
    print("interrupt cleared — the harness resumes at its next boundary")
    return 0


def cmd_resume(task_id: str | None = None, yes: bool = False,
               fresh: bool = False,
               repo: str | Path | None = None) -> int:
    """Resume a task from its last checkpoint (spec FR3), or with no
    task_id, clear an active interrupt (spec FR-3.2)."""
    if task_id is None:
        return _resume_clear_interrupt(repo=repo)
    cfg, store, runner, provider, pipeline, log = build(repo=repo)
    if _interrupt_takes_work_away(cfg, log):
        # FR-3.1: with an interrupt active, `resume <task_id>` stands down at
        # its (only) boundary by taking no work — and does not clear the file.
        return 0
    if hasattr(runner, "validate_models") and callable(getattr(runner, "validate_models", None)):
        runner.validate_models()
    return resume_task(task_id, yes, cfg, pipeline,
                       lifecycle=pipeline.lifecycle, log=log, fresh=fresh,
                       sync_engine=getattr(pipeline, "sync_engine", None))


def cmd_unpark(task_id: str, yes: bool = False, fresh: bool = False) -> int:
    """Resume/unpark a task from its last checkpoint (synonym for resume)."""
    return cmd_resume(task_id, yes=yes, fresh=fresh)


def cmd_restart(task_id: str, yes: bool = False) -> int:
    """Restart a task from scratch, dropping all checkpoints."""
    return cmd_resume(task_id, yes=yes, fresh=True)