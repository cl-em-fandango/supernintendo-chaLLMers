"""Command handlers for the harness CLI."""
from __future__ import annotations

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
from ..core.providers import Task
from ..core.stats import render_report
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
    (`requeue_stale` / `autoRequeueStaleClaims`) — see `_requeue_stale_claims`
    for why it is off.
    """
    cfg, store, runner, provider, pipeline, log = build()
    owner = _new_owner_id("run")
    _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                          enabled=_requeue_stale_enabled(cfg, requeue_stale),
                          log=log)
    claimed: list[Task] = []
    try:
        if continue_:
            resume_in_flight(pipeline.lifecycle, pipeline, log=log)
        while True:
            tasks = provider.fetch_pending(claim=True, limit=1, owner=owner)
            if not tasks:
                log("pending queue empty")
                break
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
    tasks = provider.fetch_pending(claim=True, owner=owner)
    if not tasks:
        log("no pending tasks to claim")
        return 0
    task = tasks[0]
    log(f"processing {task.id} ({len(tasks)} claimed this cycle)")
    pipeline.process(task)
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
    moved here. Stale claims: `_requeue_stale_claims`.
    """
    cfg, store, runner, provider, pipeline, log = build()
    owner = _new_owner_id("run-task-loop")
    _requeue_stale_claims(provider, CLAIM_STALE_HOURS,
                          enabled=_requeue_stale_enabled(cfg, requeue_stale),
                          log=log)
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
    """One claim selected for requeue, with the age that selected it.

    The age is read once, at selection, so the report line shows the age the
    decision was made on rather than whatever the clock says after the move.
    """
    claim: Task
    age_hours: float

    @property
    def label(self) -> str:
        return f"{self.claim.id} ({int(self.age_hours)}h)"


def _silent(line: str = "") -> None:
    """No-op log sink, for calling the guard directly (tests, ad-hoc use)."""


def _stale_claims(provider, older_hours: float) -> list[StaleClaim]:
    """The provider's claims aged `older_hours` or more.

    A claim the provider cannot age (-1.0) is skipped, never read as old.
    """
    stale = []
    for claim in provider.list_claims():
        age = provider.claim_age_hours(claim.id)
        if age >= 0 and age >= older_hours:
            stale.append(StaleClaim(claim=claim, age_hours=age))
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
                          log=None) -> int:
    """Hand every claim aged `older_hours`+ back to pending/. Off unless enabled.

    Deliberately opt-in (decision D4): the entries sitting in claimed/ are the
    input to the human review pass, and an always-on guard would empty the
    directory on the first loop. A claim younger than `older_hours` is left
    alone either way — that is a concurrent run's, not garbage.
    Returns the number of claims moved (0 when disabled).
    """
    if log is None:
        log = _silent
    if not enabled:
        return 0
    moved = 0
    for item in _stale_claims(provider, older_hours):
        if provider.requeue_claim(item.claim) is not None:
            moved += 1
            log(f"  reclaimed stale claim: {item.label}")
    if moved:
        log(f"requeued {moved} stale claim(s) (>= {older_hours:g}h)")
    return moved


def cmd_requeue_claims(older_than: float = 0.0, dry_run: bool = False) -> int:
    """Recover stranded claims: move claimed-but-unprocessed files back to pending/.

    `--older-than` bounds the sweep (default 0.0 = every claim); `--dry-run`
    prints the plan and touches nothing. An empty claimed/ is the healthy
    case, not an error, so this returns 0 either way.
    """
    _, _, _, provider, _, log = build()
    total = len(provider.list_claims())
    stale = _stale_claims(provider, older_than)
    if dry_run:
        for item in stale:
            log(f"would requeue {item.label}")
        log(f"dry run: {len(stale)} of {total} claim(s) at or over "
            f"{older_than:g}h would move to pending/")
        return 0
    moved = 0
    for item in stale:
        if provider.requeue_claim(item.claim) is not None:
            moved += 1
            log(f"requeued {item.label}")
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


def cmd_report() -> int:
    _, store, *_ = build()
    print(render_report(store.all()))
    return 0


def cmd_resume(task_id: str, yes: bool = False) -> int:
    """Resume a task from its last checkpoint (spec FR3)."""
    cfg, store, runner, provider, pipeline, log = build()
    return resume_task(task_id, yes, cfg, pipeline,
                       lifecycle=pipeline.lifecycle, log=log)


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