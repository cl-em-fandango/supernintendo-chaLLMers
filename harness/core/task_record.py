"""The single metadata record per task: GitHub linkage + claim ownership.

One JSON document per task lives at `<queue>/.meta/<task-id>.json`, keyed by
the task id (the `_slug`-ified name the providers use as `Task.id`), never by
a task-file path. Because the path does not change on a queue transition,
there is nothing to carry along and nothing to orphan — the defect this
record replaces (`sync_sidecar.py` + `claim_metadata.py` sidecars derived
from file names) with. The `.meta` directory is dot-prefixed so no `*.md`
task enumeration ever matches it.

Record shape (RECORD_SCHEMA_VERSION):

    {"version": 1,
     "github": {"issue": N, "repo": "owner/name",
                "comment_ids": {...}, "demo": bool} | null,
     "claim":  {"owner": str, "claimed_at": float} | null}

`TaskRecord` is the shape of the record; the functions here read, write and
migrate it. Every write is read-modify-write against the current record and
targets exactly one concern, so a claim write never wipes the `github`
section and a linkage write never wipes the `claim` section (FR-D4). Writes
are atomic (temp file + `os.replace`, the `task_lifecycle.write_atomic`
posture).

Reads are fail-open (NFR-1): an absent, empty, corrupt or non-object record
reads as "unlinked / unowned", never raises. Until a task's legacy sidecars
are migrated, they are honored as lowest-precedence sources (FR-E1) and
merged into the new record lazily, on sight (FR-E2): the new record is
written first, the legacy files are removed only after it is durably in
place, and a queue with no legacy files is left untouched (FR-E4).

Both concerns are fully converted: every reader and writer of claim ownership
and of GitHub linkage resolves the record by task id, so a sight of either
legacy sidecar is folded into the record and the legacy file removed (FR-E3).
Legacy files are still *read* as lowest precedence (FR-E1), which is what lets
a queue written before this change keep working untouched.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from ..workflow.task_lifecycle import (CLAIMED_LOCATION, QUEUE_LOCATIONS,
                                       QUEUE_LOCATIONS_ALL)
from .claim_metadata import ClaimMetadata, ClaimMetadataError
from .sync_linkage import SyncLinkage
from .sync_sidecar import (
    SIDECAR_SUFFIX as _GH_SUFFIX,
    TASK_DIR_SIDECAR_NAME as _GH_DIR_NAME,
)

# The task-id-keyed record store. Dot-prefixed so no task glob sees it.
META_DIR_NAME = ".meta"
RECORD_SCHEMA_VERSION = 1

# Legacy sidecar suffixes. These names are derived from task-file paths and
# are read ONLY by the migration logic in this module; nothing else may
# derive a metadata path from a task-file name.
_CLAIM_SUFFIX = ".claim.json"


# The queue locations whose entries are task markdown (`<stem>.md`), so a
# claim is a task file too. `review/` is deliberately absent: it holds the
# terminal report, not the task.
_TASK_FILE_LOCATIONS = ("pending", CLAIMED_LOCATION)


class Concern(Enum):
    """One of the two metadata concerns a single record carries."""

    CLAIM = "claim"
    GITHUB = "github"


@dataclass
class TaskRecord:
    """All queue-routing metadata for one task.

    A `None` section means the task is unlinked (github) or unowned (claim);
    there is no third "corrupt" state — corrupt data reads as absent.
    """

    github: SyncLinkage | None = None
    claim: ClaimMetadata | None = None


@dataclass(frozen=True)
class OrphanClaim:
    """A claim record whose task markdown exists nowhere in the queue.

    The record names an owner for a task that is gone — the markdown was
    deleted outside a transition, or a crash ended the task. It is not a
    claim on any work: no fetch, board or claim listing can see it. It is
    still metadata the operator path must be able to report and clean
    (the `002-…` defect class), so it is surfaced as this named record,
    never as a `Task`.

    `claimed_at` is the record's own timestamp — the only clock an orphan
    has; a corrupt one reads 0.0 like every other defensive read.
    """

    task_id: str
    owner: str
    claimed_at: float
    record: Path


@dataclass
class _MetadataView:
    """One read of a task's metadata, in the three forms a write needs.

    `record` is what the new-schema document holds, `effective` is what the
    task's metadata actually is (the record over any legacy sidecars), and
    `raw_github` says the document carries a `github` object even when that
    object does not parse — the difference between "nothing to keep" and
    "data we must not overwrite".
    """

    record: TaskRecord
    effective: TaskRecord
    legacy_files: list[Path]
    raw_github: bool = False


def task_key(name: str) -> str:
    """The record key for a task name or id.

    Mirrors `providers._slug` exactly (the id a `Task` carries): legacy
    sidecars are keyed by the full file name, so migration must map each
    legacy file-name key to this slug key. Kept local so `core` does not
    import `providers`.
    """
    s = re.sub(r"[^a-zA-Z0-9-]+", "_", name).strip("_")
    return s[:60] or "task"


def record_path(queue_dir: Path, task_id: str) -> Path:
    """The record path for a task id (it need not exist yet)."""
    return Path(queue_dir) / META_DIR_NAME / f"{task_key(task_id)}.json"


def is_legacy_metadata_name(name: str) -> bool:
    """True when a queue-entry name is a legacy metadata sidecar file.

    Enumerations that list a queue location for *tasks* use this to skip
    sidecars still sitting there (FR-A4): until migration retires them
    they are leftovers, not work. The new record store is a dot-directory
    outside every queue location and needs no filtering. The legacy name
    shapes live in this module only (acceptance criterion 3).
    """
    return name.endswith(_GH_SUFFIX) or name.endswith(_CLAIM_SUFFIX)


# ------------------------------------------------------------------ reads


def read_record(queue_dir: Path, task_id: str) -> TaskRecord:
    """The task's record, migrating legacy sidecars on sight.

    The new record wins when both exist (FR-E1); legacy-only data is merged
    into the returned record either way (§5.5). A task with no metadata
    anywhere reads as unlinked and unowned, never raises (FR-B2). Every
    legacy file the merged record speaks for is retired (FR-E2/FR-E3).
    """
    view = _read_view(queue_dir, task_id)
    adopted = _adopted_files(view)
    if adopted:
        # Repair lazily; a failed repair leaves the legacy files in place
        # and the merged read still stands (fail-open).
        if _write_record(queue_dir, task_id, view.effective):
            _remove_legacy(adopted)
    return view.effective


def read_linkage(queue_dir: Path, task_id: str) -> SyncLinkage | None:
    """The task's GitHub linkage, resolved by task id (FR-B1, demo FR-2/FR-6).

    The one shared linkage read for every caller that holds a task id: the
    record is keyed by the task, so this finds the linkage wherever the task
    currently lives — including after its staging markdown was released — and
    migrates a legacy `X.md.gh.json` / `gh.json` on sight (FR-E2). None means
    unlinked, and title matching (sync FR-2.1) is the caller's fallback.
    """
    return read_record(queue_dir, task_id).github


def set_claim(queue_dir: Path, task_id: str, owner: str,
              claimed_at: float | None = None) -> None:
    """Record `owner` as the claim holder, preserving the `github` section.

    Raises `ClaimMetadataError` when the record cannot be written — the
    caller rolls the claim rename back (FR-D2). Legacy sidecars are folded in
    before the write and removed once it is durable, so a claim write never
    drops the task's `github` section (FR-D4).
    """
    view = _read_view(queue_dir, task_id)
    view.effective.claim = ClaimMetadata(
        owner=owner,
        claimed_at=time.time() if claimed_at is None else claimed_at)
    if not _write_record(queue_dir, task_id, view.effective):
        raise ClaimMetadataError(
            f"cannot record owner {owner!r} for task {task_id}: "
            f"record write failed")
    _remove_legacy(_adopted_files(view))


def clear_claim(queue_dir: Path, task_id: str) -> bool:
    """Drop the `claim` section (the record itself if nothing else is left).

    A failed clear must never fail the transition that asked for it (FR-D3):
    it reports False and the caller logs the anomaly.

    An unparseable `github` payload survives only while there is no claim to
    clear: once a claim has to go the record is rewritten and that payload
    goes with it, because a stale owner on a live task is the bigger harm.
    """
    try:
        view = _read_view(queue_dir, task_id)
        if view.record.claim is None and not _legacy_of(view, Concern.CLAIM):
            # Nothing to clear. A record that describes no task state at all
            # is dropped rather than left behind to be mistaken for the next
            # claim's ownership; one that still carries a `github` object —
            # even one that does not parse — is left exactly as it is, since
            # rewriting or deleting it would drop `repo`/`comment_ids` a
            # legacy read could still surface.
            if view.record.github is None and not view.raw_github:
                record_path(queue_dir, task_id).unlink(missing_ok=True)
            return True
        view.effective.claim = None
        keep = view.effective
        if keep.github is None and keep.claim is None and not view.raw_github:
            record_path(queue_dir, task_id).unlink(missing_ok=True)
        elif not _write_record(queue_dir, task_id, keep):
            return False
        _remove_legacy(_adopted_files(view))
        return True
    except OSError:
        return False


def clear_linkage(queue_dir: Path, task_id: str) -> bool:
    """Drop the `github` section (the record itself if nothing else is left).

    A task's linkage ends with the task: an inbound `snes-deleted` pass
    removes the task and its linkage together, so a record cannot go on
    claiming a deleted task belongs to a closed issue. The `claim` section is
    untouched — ending a claim is `clear_claim`'s decision.

    Fail-open like every other metadata repair (FR-D3): a failed clear
    reports False for the caller to log rather than failing the transition.
    """
    try:
        view = _read_view(queue_dir, task_id)
        if view.record.github is None and not _legacy_of(view, Concern.GITHUB):
            return True
        view.effective.github = None
        keep = view.effective
        if keep.github is None and keep.claim is None:
            record_path(queue_dir, task_id).unlink(missing_ok=True)
        elif not _write_record(queue_dir, task_id, keep):
            return False
        _remove_legacy(_adopted_files(view))
        return True
    except OSError:
        return False


def write_linkage(queue_dir: Path, task_id: str,
                  linkage: SyncLinkage) -> None:
    """Record the GitHub linkage, preserving the `claim` section (FR-D4).

    This is the linkage concern's own write: it adopts legacy linkage into
    the record and retires the legacy files it adopted, and it retires a
    legacy claim sidecar once the record holds that section too.
    """
    view = _read_view(queue_dir, task_id)
    view.effective.github = linkage
    if not _write_record(queue_dir, task_id, view.effective):
        raise OSError(f"cannot write linkage record for task {task_id}")
    _remove_legacy(_adopted_files(view))


def rekey_record(queue_dir: Path, old_id: str, new_id: str) -> None:
    """Move a task's record from one task-id key to another.

    A requeue collision suffix changes the task id (`X` -> `X-requeued`), so
    whatever still describes the task after its claim ends (a `github`
    section) must follow the task's new id and the old key must not be
    stranded pointing at a phantom (§5.2).

    A record already sitting at the new key belongs to a *different* task
    (§5.9) and nothing here can tell otherwise, so the move is skipped rather
    than merged: a merge would adopt that task's sections into the moving
    record and the caller's next clear would delete them, destroying a live
    task's ownership. The old key's record then stays where it is — stale,
    readable, and never clobbering anyone else's metadata. A failed new write
    likewise keeps everything as-is (fail-open); the old key is dropped only
    once the new one is durable.
    """
    if task_key(old_id) == task_key(new_id):
        return
    if record_path(queue_dir, new_id).exists():
        return
    record = _read_new_record(queue_dir, old_id)
    if record is None:
        return
    if not _write_record(queue_dir, new_id, record):
        return
    record_path(queue_dir, old_id).unlink(missing_ok=True)


# ------------------------------------------------------------- migration


def sweep_legacy(queue_dir: Path, dry_run: bool = False) -> list[str]:
    """Migrate every legacy sidecar in the queue, orphans included.

    Migrate-on-sight covers the tasks a normal read touches; a sidecar
    whose task markdown is gone is touched by nothing, so it needs this
    queue-level sight (FR-E2): each legacy file's file-name key is mapped
    to its slug key and migrated by task id, exactly as a live task's
    would be. Files the record adopts are retired; a file the record
    cannot speak for stays on disk — cleanup must never delete the only
    readable metadata a task has (FR-E5). Idempotent: a second sweep (or
    a queue with no legacy files) finds nothing (FR-E4).

    Returns the task keys at which a legacy file was sighted. Under
    `dry_run` nothing is written or removed and the list is the plan: the
    keys a real sweep would be run against.
    This is the defined cleanup path FR-E5 asks for; its caller is the
    operator's queue-hygiene command, not a read-only inspection.
    """
    queue_dir = Path(queue_dir)
    sightings: list[str] = []
    seen: set[str] = set()
    for location in QUEUE_LOCATIONS_ALL:
        directory = queue_dir / location
        for entry in _listing(directory):
            stem = _legacy_stem(entry)
            if stem is not None and task_key(stem) not in seen:
                seen.add(task_key(stem))
                sightings.append(stem)
            # A legacy sidecar inside a task dir is keyed by the dir name.
            if (directory / entry / _GH_DIR_NAME).is_file() \
                    and task_key(entry) not in seen:
                seen.add(task_key(entry))
                sightings.append(entry)
    if dry_run:
        return [task_key(name) for name in sightings]
    migrated = []
    for name in sightings:
        if _migrate_legacy(queue_dir, name) is not None:
            migrated.append(task_key(name))
    return migrated


def list_orphan_claims(queue_dir: Path) -> list[OrphanClaim]:
    """Every claim record whose task markdown is gone from the queue (§5.8).

    A readable `claim` section — in the record store or, for a task no read
    has migrated yet, in a legacy `X.md.claim.json` — with no task file
    (`<stem>.md` in `pending/` or `claimed/`) and no task directory
    (`active/` or a terminal location) slugging to its key, describes no
    work anywhere. Review summaries are task *reports*, not tasks, so they
    do not keep a claim alive; the linkage section is irrelevant here —
    linkage outlives the markdown by design (§5.4), only a *claim* without a
    task is an orphan.
    """
    queue_dir = Path(queue_dir)
    meta = queue_dir / META_DIR_NAME
    try:
        names = sorted(p.name for p in meta.iterdir()
                       if p.name.endswith(".json"))
    except OSError:
        names = []  # no record store yet: legacy sidecars are the only view
    orphans: list[OrphanClaim] = []
    keys: set[str] = set()
    for name in names:
        task_id = name[:-len(".json")]
        record = _read_new_record(queue_dir, task_id)
        if record is None or record.claim is None:
            continue
        keys.add(task_key(task_id))
        if _task_in_queue(queue_dir, task_key(task_id)):
            continue
        orphans.append(OrphanClaim(
            task_id=task_id, owner=record.claim.owner,
            claimed_at=record.claim.claimed_at,
            record=record_path(queue_dir, task_id)))
    orphans.extend(_legacy_orphan_claims(queue_dir, keys))
    return orphans


def _legacy_orphan_claims(queue_dir: Path, seen: set[str]) -> list[OrphanClaim]:
    """Orphan claims that still only exist as legacy sidecars.

    A `--dry-run` hygiene pass must report what it *would* clean without
    migrating anything, so an orphan whose ownership data is still a
    `X.md.claim.json` is read straight off the legacy file (FR-E1) rather
    than through a record that does not exist yet. `seen` holds the keys the
    record scan already reported, so a task never appears twice.
    """
    orphans: list[OrphanClaim] = []
    reported: set[str] = set(seen)
    for location in QUEUE_LOCATIONS_ALL:
        directory = queue_dir / location
        for name in _listing(directory):
            if not name.endswith(_CLAIM_SUFFIX):
                continue
            stem = _legacy_stem(name) or ""
            key = task_key(stem)
            if key in reported or _task_in_queue(queue_dir, key):
                continue
            reported.add(key)
            claim = _read_legacy_claim(directory / name)
            if claim is None:
                continue
            orphans.append(OrphanClaim(
                task_id=stem, owner=claim.owner,
                claimed_at=claim.claimed_at,
                record=record_path(queue_dir, stem)))
    return orphans


def _task_in_queue(queue_dir: Path, key: str) -> bool:
    """True when a task file or task dir slugging to `key` sits in a location.

    File-shaped locations hold task markdown (`<stem>.md`); dir-shaped
    locations hold a task directory named after the task. Dot-prefixed
    entries are metadata litter, never tasks (FR-A4).
    """
    for location in _TASK_FILE_LOCATIONS:
        for entry in _listing(queue_dir / location):
            if entry.endswith(".md") and task_key(entry[:-3]) == key:
                return True
    for location in QUEUE_LOCATIONS:
        directory = queue_dir / location
        for entry in _listing(directory):
            if (not entry.startswith(".") and task_key(entry) == key
                    and (directory / entry).is_dir()):
                return True
    return False


def _migrate_legacy(queue_dir: Path, task_id: str) -> TaskRecord | None:
    """Merge every legacy sidecar of `task_id` into the new record.

    Returns the merged record when legacy files exist (and removes only the
    retired concerns' files, and only after the new record is durably
    written), None when there is nothing to migrate. Idempotent: a second run
    finds no legacy files to retire (FR-E4).
    """
    view = _read_view(queue_dir, task_id)
    if not view.legacy_files:
        return None
    adopted = _adopted_files(view)
    if adopted and _write_record(queue_dir, task_id, view.effective):
        _remove_legacy(adopted)
    return view.effective


def _read_view(queue_dir: Path, task_id: str) -> _MetadataView:
    """The record on disk, the legacy-overlayed view, and the legacy paths.

    New-record sections win on disagreement (FR-E1/§5.6); a legacy section
    is adopted only where the new record has none. Task dirs come first in
    the legacy file list, so a terminal dir's `gh.json` shadows a same-named
    review file's sidecar.
    """
    payload = _read_payload(queue_dir, task_id)
    stored = _record_from_payload(payload) if payload is not None \
        else TaskRecord()
    files = _find_legacy_files(queue_dir, task_id)
    effective = TaskRecord(github=stored.github, claim=stored.claim)
    for path in files:
        concern = _concern_of(path)
        if concern is Concern.GITHUB and effective.github is None:
            effective.github = _read_legacy_github(path)
        elif concern is Concern.CLAIM and effective.claim is None:
            effective.claim = _read_legacy_claim(path)
    return _MetadataView(
        record=stored,
        effective=effective,
        legacy_files=files,
        raw_github=isinstance((payload or {}).get("github"), dict))


def _adopted_files(view: _MetadataView) -> list[Path]:
    """The legacy files the record now holds, so they may be removed.

    A legacy file the record cannot speak for — unreadable, or holding a
    section the record did not adopt — stays on disk. It is still the only
    trace of that data, and removing it would lose the audit trail FR-E5
    needs to clean leftovers safely.
    """
    adopted: list[Path] = []
    for path in view.legacy_files:
        concern = _concern_of(path)
        if concern is Concern.GITHUB and view.effective.github is not None:
            adopted.append(path)
        elif concern is Concern.CLAIM and view.effective.claim is not None:
            adopted.append(path)
    return adopted


def _legacy_of(view: _MetadataView, concern: Concern) -> list[Path]:
    """The legacy files of one concern, readable or not."""
    return [p for p in view.legacy_files if _concern_of(p) is concern]


def _find_legacy_files(queue_dir: Path, task_id: str) -> list[Path]:
    """Every legacy sidecar on disk whose file-name key slugs to `task_id`.

    Task dirs are searched before task files (the legacy precedence: a
    terminal dir wins over a same-named review file). The
    file-name key is the full stem, so a name whose slug differs from its
    stem still maps to the right record.
    """
    queue_dir = Path(queue_dir)
    key = task_key(task_id)
    found: list[Path] = []
    for location in QUEUE_LOCATIONS_ALL:
        dir_sidecar = queue_dir / location / task_id / _GH_DIR_NAME
        if dir_sidecar.is_file():
            found.append(dir_sidecar)
    for location in QUEUE_LOCATIONS_ALL:
        directory = queue_dir / location
        for name in _listing(directory):
            stem = _legacy_stem(name)
            if stem is not None and task_key(stem) == key:
                found.append(directory / name)
    return found


@lru_cache(maxsize=32)
def _listing_cached(directory: str, mtime_ns: int) -> tuple[str, ...]:
    try:
        return tuple(sorted(p.name for p in Path(directory).iterdir()))
    except OSError:
        return ()


def _listing(directory: Path) -> tuple[str, ...]:
    """The entry names of a queue location, cached until it changes.

    Every `read_record` scans all seven queue locations for legacy sidecars,
    so listing a queue of N tasks re-read the same seven directories N times.
    A directory's mtime moves with every entry added or removed, so
    (directory, mtime) is a sound key for a scan that only needs names.
    """
    try:
        stamp = directory.stat().st_mtime_ns
    except OSError:
        return ()
    return _listing_cached(str(directory), stamp)


def _legacy_stem(name: str) -> str | None:
    """The task-file stem a legacy sidecar name is derived from."""
    for suffix in (_GH_SUFFIX, _CLAIM_SUFFIX):
        if name.endswith(suffix):
            stem = name[:-len(suffix)]
            return stem[:-3] if stem.endswith(".md") else stem
    return None


def _concern_of(path: Path) -> Concern | None:
    """Which metadata concern a legacy sidecar belongs to."""
    if path.name.endswith(_CLAIM_SUFFIX):
        return Concern.CLAIM
    if path.name.endswith(_GH_SUFFIX) or path.name == _GH_DIR_NAME:
        return Concern.GITHUB
    return None


def _read_legacy_github(path: Path) -> SyncLinkage | None:
    """One legacy `gh.json`; None when unreadable (title-match fallback)."""
    try:
        raw = json.loads(path.read_text())
        return SyncLinkage(
            issue=int(raw["issue"]),
            repo=str(raw.get("repo", "")),
            comment_ids=dict(raw.get("comment_ids") or {}),
            demo=bool(raw.get("demo", False)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_legacy_claim(path: Path) -> ClaimMetadata | None:
    """One legacy `claim.json`; None when it holds no usable owner."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    owner = payload.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    return ClaimMetadata(owner=owner,
                         claimed_at=_coerce_timestamp(
                             payload.get("claimed_at")))


def _remove_legacy(legacy_files: list[Path]) -> None:
    for path in legacy_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A leftover legacy file is shadowed by the durable new record
            # (new wins) and retried on the next sight; it must not fail
            # the operation that migrated it.
            pass


# ------------------------------------------------------- record payloads


def _read_payload(queue_dir: Path, task_id: str) -> dict | None:
    """The record document as written; None when absent or not an object."""
    try:
        payload = json.loads(record_path(queue_dir, task_id).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_new_record(queue_dir: Path, task_id: str) -> TaskRecord | None:
    """The new-schema record; None when absent, corrupt or not an object."""
    payload = _read_payload(queue_dir, task_id)
    if payload is None:
        return None
    return _record_from_payload(payload)


def _record_from_payload(payload: dict) -> TaskRecord:
    return TaskRecord(github=_parse_github(payload.get("github")),
                      claim=_parse_claim(payload.get("claim")))


def _parse_github(raw: object) -> SyncLinkage | None:
    if not isinstance(raw, dict):
        return None
    try:
        issue = int(raw["issue"])
    except (KeyError, ValueError, TypeError):
        return None
    return SyncLinkage(
        issue=issue,
        repo=str(raw.get("repo", "")),
        comment_ids=dict(raw.get("comment_ids") or {}),
        demo=bool(raw.get("demo", False)),
    )


def _parse_claim(raw: object) -> ClaimMetadata | None:
    if not isinstance(raw, dict):
        return None
    owner = raw.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    return ClaimMetadata(owner=owner,
                         claimed_at=_coerce_timestamp(raw.get("claimed_at")))


def _coerce_timestamp(value: object) -> float:
    """A usable `claimed_at`; anything non-numeric (or a bool) is 0.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _github_payload(linkage: SyncLinkage) -> dict:
    return {
        "issue": linkage.issue,
        "repo": linkage.repo,
        "comment_ids": dict(linkage.comment_ids),
        "demo": linkage.demo,
    }


def _claim_payload(claim: ClaimMetadata) -> dict:
    return {"owner": claim.owner, "claimed_at": claim.claimed_at}


def _write_record(queue_dir: Path, task_id: str, record: TaskRecord) -> bool:
    """Atomically write the whole record; False when the write failed.

    The caller decides what a failure means: `set_claim` raises so the claim
    rename rolls back (FR-D2); a read-path repair just keeps the legacy
    files and retries on the next sight.
    """
    dest = record_path(queue_dir, task_id)
    payload = {
        "version": RECORD_SCHEMA_VERSION,
        "github": _github_payload(record.github)
                  if record.github is not None else None,
        "claim": _claim_payload(record.claim)
                 if record.claim is not None else None,
    }
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload) + "\n")
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True
