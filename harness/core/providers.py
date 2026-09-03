"""Task provider adapters.

A TaskProvider is the single integration point for where tasks come from.
The pipeline only ever talks to this interface, so new sources (GitHub
issues, a database, an API, ...) can be added without touching the pipeline.

Interface:
    fetch_pending() -> list[Task]     # tasks ready to work on
    count_pending() -> int            # how many a fetch would hand over, read-only
    list_claims() -> list[Task]       # tasks held claimed but not yet processed
    list_owned_claims() -> list[Claim]  # same claims, with who holds each
    requeue_all_claims() -> list[str] # hand every claim back to the pending pool
    submit(task: Task) -> None        # (optional) push results back upstream

A claim is a fetch side effect, so recovering from one is the provider's job:
sources with no claim concept inherit empty defaults and stay valid adapters.

Task is a plain dataclass: id + freeform markdown body + optional source hint.
Claim is one held task plus its ownership record; ownership metadata lives in
the task's single metadata record (`task_record.py`) and is what keeps one
invocation's cleanup from handing back another invocation's claim.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from . import task_record
from .claim_metadata import OWNER_UNKNOWN, ClaimMetadataError
from .enqueue_guard import check_enqueue

# The `Task.meta` key carrying the demo-request flag (demo spec FR-1.4).
DEMO_META_KEY = "demo"


def _meta_from_record(record: "task_record.TaskRecord") -> dict:
    """A Task's `meta` off the task's metadata record (demo spec FR-1.4).

    The demo flag is lifted out of the record's `github` section here so the
    pipeline never has to read queue metadata itself. An unflagged task gets
    an empty meta, exactly the shape it had before the demo feature.
    """
    linkage = record.github
    return {DEMO_META_KEY: True} if linkage is not None and linkage.demo else {}


def _task_meta(queue_dir: Path, task_id: str) -> dict:
    """A Task's `meta` resolved by task id through the record API (FR-B1)."""
    return _meta_from_record(task_record.read_record(queue_dir, task_id))


def task_meta(queue_dir: Path, task_id: str) -> dict:
    """Public entry to the record-backed `Task.meta` for one task id.

    Used by CLI and resume entry points that build a `Task` directly so a
    task's demo flag from its metadata record survives into the pipeline
    exactly as it does for provider-claimed tasks (FR-1.4).
    """
    return _task_meta(queue_dir, task_id)


@dataclass
class Task:
    id: str
    body: str
    source: str = ""            # e.g. "directory", "github:owner/repo#123"
    # Freeform provider-supplied facts. The demo flag lives here as
    # `meta["demo"]` (True only on a demo request) — demo spec FR-1.4.
    meta: dict = field(default_factory=dict)


@dataclass
class Claim:
    """One held claim: the task, the file it was claimed from, and who holds it.

    `owner` is `OWNER_UNKNOWN` when the task's record carries no readable
    `claim` section — a claim taken before ownership existed, or one whose
    record was lost or corrupted. `meta_path` is the record path.
    """

    task: Task
    filename: str
    owner: str = OWNER_UNKNOWN
    claimed_at: float = 0.0
    meta_path: Path | None = None


class TaskProvider(ABC):
    """Adapter interface for task sources."""

    name: str = "abstract"

    @abstractmethod
    def fetch_pending(self) -> list[Task]:
        """Return tasks ready to be worked on, in priority order."""

    def count_pending(self) -> int:
        """How many tasks `fetch_pending()` would return, without claiming.

        A count is not a fetch: a caller that only wants the queue depth must
        not ask a fetch for it, because a fetch is where the claim lifecycle
        lives and a default flipped there turns a question into a claim. The
        default answers honestly by asking the fetch — an adapter whose fetch
        is cheap and side-effect-free has nothing to lose by overriding.
        """
        return len(self.fetch_pending())

    def list_claims(self) -> list[Task]:
        """Tasks currently claimed but not yet processed. Sources without a
        claim lifecycle have none, so the default is empty."""
        return []

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None,
                      force: bool = False) -> str | None:
        """Hand one claim back to the pending pool. Returns the new path, or
        None when this provider cannot requeue (no claim lifecycle) or `owner`
        does not hold it. `force` is the operator override documented on
        `DirectoryTaskProvider.requeue_claim`; a run never sets it."""
        return None

    def requeue_all_claims(self, owner: str | None = None) -> list[str]:
        """Hand every claim back to the pending pool. Returns the moved names.
        With an `owner`, only that owner's claims move."""
        return []

    def list_owned_claims(self) -> list[Claim]:
        """Held claims with their recorded ownership. Sources without a claim
        lifecycle have none, so the default is empty."""
        return []

    def sweep_legacy_metadata(self, dry_run: bool = False) -> list[str]:
        """Migrate this source's legacy metadata sidecars into the record
        store; under `dry_run` report the plan and write nothing. Sources
        with no legacy metadata history have none, so the default is
        empty."""
        return []

    def list_orphan_claims(self) -> list["task_record.OrphanClaim"]:
        """Claim records whose task markdown exists nowhere. Sources
        without a claim lifecycle have none, so the default is empty."""
        return []

    def clean_orphan_claim(self, orphan: "task_record.OrphanClaim") -> bool:
        """Drop an orphan claim record's `claim` section (the record too
        when nothing else is left). False when the clear failed; sources
        without a claim lifecycle clean nothing."""
        return False

    def claim_age_hours(self, name: str) -> float:
        """Age in hours of a claim; -1.0 when there is no such claim."""
        return -1.0

    def submit(self, task: Task, status: str, summary: str) -> None:
        """Optional: report a finished task back to the source. No-op by default."""


class DirectoryTaskProvider(TaskProvider):
    """Tasks are markdown files dropped into a pending directory.

    One file = one task. Filename (sans .md) becomes the task id.

    Lifecycle is file-based: a task is CLAIMED by moving pending/X.md into
    active/ at fetch time, so a parked/failed/done task can never be
    re-claimed. The pipeline then moves the active/ dir to its terminal
    folder. To retry a parked task, move it back to pending/ (see `unpark`).

    A claim is this provider's own side effect, so claim recovery lives here
    too: `list_claims()`, `requeue_claim()`, `requeue_all_claims()` and
    `claim_age_hours()` see and undo claims without touching the queue layout.

    Ownership is opt-in per fetch: `fetch_pending(claim=True, owner=id)` writes
    the task's metadata record (see `task_record.py`), and a requeue
    that names an `owner` only moves claims recorded against that owner. A
    requeue with no `owner` is the pre-ownership call and checks nothing; a
    forced requeue skips the gate on an operator's authority, which is a
    decision no run invocation gets to make (see `requeue_claim`).

    Files the enqueue guard refuses (plan parents marked `DO NOT EXECUTE`) are
    never fetched and never claimed: they stay untouched in pending/ as the
    requirement archives they are, and each skip is logged with the leaf ids to
    enqueue instead.
    """

    name = "directory"

    def __init__(self, pending_dir: str | Path, claimed_dir: str | Path | None = None,
                 log=print):
        self.pending_dir = Path(pending_dir)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.log = log
        # Claimed files are staged here (out of pending/) so they are not
        # re-fetched. The pipeline reads the body at claim time, then the
        # staging file is removed once intake has copied it into the task dir.
        self.claimed_dir = Path(claimed_dir) if claimed_dir else self.pending_dir.parent / "claimed"
        self.claimed_dir.mkdir(parents=True, exist_ok=True)
        # Root of the task-id-keyed metadata record store (`<queue>/.meta/`).
        # The record travels with the task, not the file, so no transition
        # needs to know it exists — only this root does.
        self.queue_dir = self.pending_dir.parent

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None,
                      owner: str | None = None) -> list[Task]:
        """List pending tasks. With claim=True, move each pending/X.md into
        claimed/ so it is not seen again until explicitly requeued.

        `limit` caps how many tasks are returned, so a caller that asks for
        one claim gets one instead of the whole queue; whatever it did not
        take stays in pending/. Default None is every task, as before.

        With a non-empty `owner`, every claim taken here is recorded against
        that owner id. If the record cannot be written the markdown is moved
        back to pending/ and `ClaimMetadataError` is raised — a claim nobody
        owns is worse than no claim, because no cleanup would reclaim it.

        A file the enqueue guard refuses is skipped: it costs no `limit` slot,
        is not moved into claimed/, and its skip is logged once per fetch.
        """
        tasks = []
        for f in sorted(self.pending_dir.glob("*.md")):
            if limit is not None and len(tasks) >= limit:
                break
            tid = _slug(f.stem)
            body = f.read_text()
            decision = check_enqueue(body, f.name)
            if not decision.allowed:
                self.log(f"  ⚠ not enqueuing {decision.reason}")
                continue
            if claim:
                dest = self.claimed_dir / f.name
                try:
                    f.rename(dest)
                except OSError:
                    # already claimed by a concurrent run; skip
                    continue
                if owner:
                    try:
                        task_record.set_claim(self.queue_dir, tid, owner)
                    except ClaimMetadataError:
                        if self._move_to_pending(dest) is None:
                            self.log(f"  ⚠ {dest.name} is claimed with no owner "
                                     f"and could not be rolled back")
                        raise
            tasks.append(Task(id=tid, body=body, source=f"directory:{f.name}",
                              meta=_task_meta(self.queue_dir, tid)))
        return tasks

    def count_pending(self) -> int:
        """Pending files a fetch would hand over, counted without claiming.

        The same rule as `fetch_pending()` applies: a file the enqueue guard
        refuses is not a task, so it is not counted either. The autonomous
        generator stops on this number, and it must stop on the queue it can
        actually work, not on a pile of plan parents no fetch would claim.

        Read-only by construction — no rename, no sidecar, no write, and no
        call into `fetch_pending()`. The generator asks once per attempt, so a
        count that ever took a claim would empty the queue by looking at it.
        A refusal is not logged either: this is asked several times per
        proposal, and the skip is the fetch's news to tell once.
        """
        return sum(1 for f in self.pending_dir.glob("*.md")
                   if check_enqueue(f.read_text(), f.name).allowed)

    def list_claims(self) -> list[Task]:
        """The files sitting in claimed/, in sorted order, as tasks."""
        return [Task(id=_slug(f.stem), body=f.read_text(),
                     source=f"claimed:{f.name}",
                     meta=_task_meta(self.queue_dir, _slug(f.stem)))
                for f in sorted(self.claimed_dir.glob("*.md"))]

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None,
                      force: bool = False) -> str | None:
        """Move one claimed file back to pending/, by filename or Task.

        Returns the new path as a string, or None when there is no such claim
        or `owner` does not hold it. A pending/ name collision gets a
        `-requeued` suffix; nothing is ever overwritten.

        Naming an `owner` makes the requeue ownership-checked: a caller may
        hand back only claims its own invocation took. Claims whose record is
        missing or corrupt read as `OWNER_UNKNOWN` and are refused too — an
        operator, not a run, decides what happens to them.

        `force=True` skips that gate. It exists for the one caller holding an
        operator's authority rather than an owner id — `harness.py
        requeue-claims` — and it is what lets an unattributable claim be handed
        back at all. A run command must never set it: a forced requeue can move
        a claim another live invocation is working on, which is the whole thing
        ownership was added to prevent. The caller reads the records itself
        (`list_owned_claims()`) and prints the owner it overrode.
        """
        src = self._claim_path(name_or_task)
        if src is None:
            return None
        if not force and owner is not None and not self._owner_matches(src, owner):
            held = task_record.read_record(
                self.queue_dir, _slug(src.stem)).claim
            self.log(f"  ⚠ not requeueing {src.name}: held by "
                     f"{held.owner if held else OWNER_UNKNOWN}, not {owner}")
            return None
        return self._move_to_pending(src)

    def requeue_all_claims(self, owner: str | None = None) -> list[str]:
        """Move every claimed file back to pending/. Returns the moved names.

        With an `owner`, only that owner's claims move; foreign, unknown and
        corrupt claims stay claimed for their holder or an operator.
        """
        moved = []
        for f in sorted(self.claimed_dir.glob("*.md")):
            if owner is not None and not self._owner_matches(f, owner):
                continue
            if self._move_to_pending(f) is not None:
                moved.append(f.name)
        return moved

    def list_owned_claims(self) -> list[Claim]:
        """Every held claim with its recorded ownership, in sorted order.

        `list_claims()` stays the plain task view; this is the ownership view
        a cleanup or audit path needs. A claim with no readable `claim`
        section is reported with `owner=OWNER_UNKNOWN`, never dropped.
        """
        claims = []
        for f in sorted(self.claimed_dir.glob("*.md")):
            tid = _slug(f.stem)
            record = task_record.read_record(self.queue_dir, tid)
            task = Task(id=tid, body=f.read_text(),
                        source=f"directory:{f.name}",
                        meta=_meta_from_record(record))
            claim = record.claim
            claims.append(Claim(
                task=task, filename=f.name,
                owner=claim.owner if claim is not None else OWNER_UNKNOWN,
                claimed_at=claim.claimed_at if claim is not None else 0.0,
                meta_path=task_record.record_path(self.queue_dir, tid)))
        return claims

    def sweep_legacy_metadata(self, dry_run: bool = False) -> list[str]:
        """Migrate every legacy sidecar in the queue, orphans included.

        Under `dry_run` the sweep only names the tasks it would migrate:
        an inspection run must leave the queue byte-identical.

        The operator's queue-hygiene path (`requeue-claims`) calls this so
        a sidecar whose markdown is gone — sighted by no task read — is
        still migrated by task id and retired (FR-E2/FR-E5). Returns the
        task keys sighted; a legacy file the record cannot speak for is
        left in place (it stays the task's only readable metadata).
        """
        return task_record.sweep_legacy(self.queue_dir, dry_run=dry_run)

    def list_orphan_claims(self) -> list[task_record.OrphanClaim]:
        """Claim records whose task markdown exists nowhere (§5.8).

        These are never claims on work: `list_claims`, `fetch_pending` and
        the board all enumerate markdown and cannot see them (FR-A4).
        They are reported and cleaned through this separate view so an
        operator can see and remove what no run can act on.
        """
        return task_record.list_orphan_claims(self.queue_dir)

    def clean_orphan_claim(self, orphan: task_record.OrphanClaim) -> bool:
        """End an orphan claim: clear its `claim` section, keeping any
        `github` section (linkage outlives the markdown, §5.4). A failed
        clear reports False for the caller to log (FR-D3)."""
        if not task_record.clear_claim(self.queue_dir, orphan.task_id):
            self.log(f"  ⚠ orphan claim record for {orphan.task_id} could "
                     f"not be cleaned")
            return False
        return True

    def claim_age_hours(self, name: str) -> float:
        """Hours since a claimed file was last written; -1.0 if not claimed.

        A file that has already been handed back keeps its claim mtime, so
        the pending copy is aged too — a name only reads as absent (-1.0) when
        it is in neither claimed/ nor pending/.
        """
        src = _named_file(self.claimed_dir, name) or _named_file(self.pending_dir, name)
        if src is None:
            return -1.0
        return (time.time() - src.stat().st_mtime) / 3600.0

    def _claim_path(self, name_or_task: "str | Task") -> Path | None:
        """Resolve a filename, a bare stem, or a Task to its claimed file."""
        if isinstance(name_or_task, Task):
            return next((f for f in sorted(self.claimed_dir.glob("*.md"))
                         if _slug(f.stem) == name_or_task.id), None)
        return _named_file(self.claimed_dir, name_or_task)

    def _move_to_pending(self, src: Path) -> str | None:
        """Move one claimed file into pending/, never overwriting. Path str.

        The claim record belongs to the claim, not to the file's location, so
        its `claim` section is dropped once the claim is gone — a record that
        still named an owner would misattribute the next claim taken on that
        name. `_retire_claim_record` does the bookkeeping, including the
        re-key a `-requeued` collision suffix implies.
        """
        dest = self.pending_dir / src.name
        n = 0
        while dest.exists():
            n += 1
            tag = "-requeued" if n == 1 else f"-requeued-{n - 1}"
            dest = self.pending_dir / f"{src.stem}{tag}.md"
        try:
            src.rename(dest)
        except OSError:
            # vanished or held elsewhere: leave it claimed rather than lose it
            return None
        self._retire_claim_record(src, dest)
        return str(dest)

    def _retire_claim_record(self, src: Path, dest: Path) -> None:
        """End the claim record once the markdown has moved back to pending/.

        The claim being ended belongs to the id the claim was taken under,
        so it is cleared at the *source* id. Only what survives the claim (a
        `github` section) is then re-keyed onto the `-requeued` id the task
        has taken, so it follows the task and the old key is not stranded
        (§5.2). Clearing at the destination id instead would hand back — or
        delete — the claim of whichever task already owns that id (§5.9), and
        a re-key is skipped when the clear failed rather than moving a live
        claim onto a new name. A failed clear must not fail the requeue
        (FR-D3): it is logged as an anomaly.
        """
        old_id, new_id = _slug(src.stem), _slug(dest.stem)
        if not task_record.clear_claim(self.queue_dir, old_id):
            self.log(f"  ⚠ claim record for {old_id} could not be cleared")
            return
        if old_id != new_id:
            task_record.rekey_record(self.queue_dir, old_id, new_id)

    def _owner_matches(self, claim_file: Path, owner: str) -> bool:
        """True when the task's record names exactly `owner` as claim holder."""
        claim = task_record.read_record(
            self.queue_dir, _slug(claim_file.stem)).claim
        return claim is not None and claim.owner == owner

    def release_claim(self, task: Task) -> None:
        """Remove the staging file (and the record's `claim` section) once
        intake has persisted the body."""
        for f in self.claimed_dir.glob("*.md"):
            if _slug(f.stem) == task.id:
                f.unlink(missing_ok=True)
                if not task_record.clear_claim(self.queue_dir, task.id):
                    self.log(f"  ⚠ claim record for {task.id} could not be "
                             f"cleared")
                break

    def submit(self, task: Task, status: str, summary: str) -> None:
        # Directory provider lifecycle is managed by the pipeline moving files
        # between queue subdirectories; nothing to push back upstream.
        pass


def _named_file(directory: Path, name: str) -> Path | None:
    """`directory/<name>.md` if it exists; `name` may already carry the .md.

    Falls back to a slug match: a `Task.id` is `_slug`-ified (and truncated at
    60 chars), so the id a caller got from `list_claims()` is not always the
    file's stem, and the id is all it has to look a claim up with.
    """
    filename = Path(str(name)).name
    stem = filename[:-3] if filename.endswith(".md") else filename
    candidate = directory / f"{stem}.md"
    if candidate.is_file():
        return candidate
    slug = _slug(stem)
    return next((f for f in sorted(directory.glob("*.md"))
                 if _slug(f.stem) == slug), None)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")
    return s[:60] or "task"


def create_provider(cfg) -> TaskProvider:
    """Factory: build the configured provider. Add new adapters here."""
    kind = cfg.task_provider
    if kind == "directory":
        pending = cfg.directory_provider.get(
            "pendingDir", str(cfg.queue_dir / "pending"))
        claimed = cfg.directory_provider.get(
            "claimedDir", str(cfg.queue_dir / "claimed"))
        return DirectoryTaskProvider(pending, claimed)
    raise ValueError(f"unknown task provider: {kind!r}")
