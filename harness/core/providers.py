"""Task provider adapters.

A TaskProvider is the single integration point for where tasks come from.
The pipeline only ever talks to this interface, so new sources (GitHub
issues, a database, an API, ...) can be added without touching the pipeline.

Interface:
    fetch_pending() -> list[Task]     # tasks ready to work on
    list_claims() -> list[Task]       # tasks held claimed but not yet processed
    list_owned_claims() -> list[Claim]  # same claims, with who holds each
    requeue_all_claims() -> list[str] # hand every claim back to the pending pool
    submit(task: Task) -> None        # (optional) push results back upstream

A claim is a fetch side effect, so recovering from one is the provider's job:
sources with no claim concept inherit empty defaults and stay valid adapters.

Task is a plain dataclass: id + freeform markdown body + optional source hint.
Claim is one held task plus its ownership record; ownership metadata lives in
`claim_metadata.py` and is what keeps one invocation's cleanup from handing
back another invocation's claim.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .claim_metadata import (
    OWNER_UNKNOWN,
    ClaimMetadataError,
    metadata_path,
    read_metadata,
    remove_metadata,
    write_metadata,
)
from .enqueue_guard import check_enqueue


@dataclass
class Task:
    id: str
    body: str
    source: str = ""            # e.g. "directory", "github:owner/repo#123"
    meta: dict = field(default_factory=dict)


@dataclass
class Claim:
    """One held claim: the task, the file it was claimed from, and who holds it.

    `owner` is `OWNER_UNKNOWN` when the claim carries no readable ownership
    sidecar — a claim taken before ownership existed, or one whose sidecar was
    lost or corrupted.
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
    a sidecar beside each claimed file (see `claim_metadata.py`), and a requeue
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

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None,
                      owner: str | None = None) -> list[Task]:
        """List pending tasks. With claim=True, move each pending/X.md into
        claimed/ so it is not seen again until explicitly requeued.

        `limit` caps how many tasks are returned, so a caller that asks for
        one claim gets one instead of the whole queue; whatever it did not
        take stays in pending/. Default None is every task, as before.

        With a non-empty `owner`, every claim taken here is recorded against
        that owner id. If a sidecar cannot be written the markdown is moved
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
                        write_metadata(dest, owner)
                    except ClaimMetadataError:
                        if self._move_to_pending(dest) is None:
                            self.log(f"  ⚠ {dest.name} is claimed with no owner "
                                     f"and could not be rolled back")
                        raise
            tasks.append(Task(id=tid, body=body, source=f"directory:{f.name}"))
        return tasks

    def list_claims(self) -> list[Task]:
        """The files sitting in claimed/, in sorted order, as tasks."""
        return [Task(id=_slug(f.stem), body=f.read_text(), source=f"claimed:{f.name}")
                for f in sorted(self.claimed_dir.glob("*.md"))]

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None,
                      force: bool = False) -> str | None:
        """Move one claimed file back to pending/, by filename or Task.

        Returns the new path as a string, or None when there is no such claim
        or `owner` does not hold it. A pending/ name collision gets a
        `-requeued` suffix; nothing is ever overwritten.

        Naming an `owner` makes the requeue ownership-checked: a caller may
        hand back only claims its own invocation took. Claims whose sidecar is
        missing or corrupt read as `OWNER_UNKNOWN` and are refused too — an
        operator, not a run, decides what happens to them.

        `force=True` skips that gate. It exists for the one caller holding an
        operator's authority rather than an owner id — `harness.py
        requeue-claims` — and it is what lets an unattributable claim be handed
        back at all. A run command must never set it: a forced requeue can move
        a claim another live invocation is working on, which is the whole thing
        ownership was added to prevent. The caller reads the sidecar itself
        (`list_owned_claims()`) and prints the owner it overrode.
        """
        src = self._claim_path(name_or_task)
        if src is None:
            return None
        if not force and owner is not None and not self._owner_matches(src, owner):
            self.log(f"  ⚠ not requeueing {src.name}: held by "
                     f"{read_metadata(src).owner}, not {owner}")
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
        a cleanup or audit path needs. A claim with no readable sidecar is
        reported with `owner=OWNER_UNKNOWN`, never dropped.
        """
        claims = []
        for f in sorted(self.claimed_dir.glob("*.md")):
            meta = read_metadata(f)
            task = Task(id=_slug(f.stem), body=f.read_text(),
                        source=f"claimed:{f.name}")
            claims.append(Claim(task=task, filename=f.name, owner=meta.owner,
                                claimed_at=meta.claimed_at,
                                meta_path=metadata_path(f)))
        return claims

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

        The ownership sidecar belongs to the claim, not to the file's location,
        so it is dropped once the claim is gone: the markdown rename leaves it
        behind in claimed/, and a sidecar with no claim under it would
        misattribute the next claim taken on that name.
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
        remove_metadata(src)
        return str(dest)

    def _owner_matches(self, claim_file: Path, owner: str) -> bool:
        """True when the sidecar beside `claim_file` records exactly `owner`."""
        return read_metadata(claim_file).owner == owner

    def release_claim(self, task: Task) -> None:
        """Remove the staging file (and its ownership sidecar) once intake has
        persisted the body."""
        for f in self.claimed_dir.glob("*.md"):
            if _slug(f.stem) == task.id:
                f.unlink(missing_ok=True)
                remove_metadata(f)
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
