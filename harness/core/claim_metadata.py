"""Claim ownership metadata: the JSON sidecar that records who holds a claim.

A claim is recorded twice. The markdown file itself is the claim (it was moved
out of `pending/`), and the sidecar says which invocation took it and when.
Without the sidecar, a cleanup path cannot tell its own claim from one held by
another live invocation, so it either steals work or leaves its own behind.

Layout: `claimed/<name>.md.claim.json` beside `claimed/<name>.md`. The sidecar
deliberately does not end in `.md`, so every claim listing that globs markdown
keeps seeing exactly the claims and nothing else.

Missing or unreadable metadata is not an error at this layer: it reads back as
`OWNER_UNKNOWN` so a caller can report it and decide what to do. Writing is
atomic — a temp file in the claim directory plus `os.replace` — so a crash
mid-write cannot leave a half-written owner behind.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

OWNER_UNKNOWN = "unknown"      # owner of a claim whose sidecar is absent or corrupt

SCHEMA_VERSION = 1

_SUFFIX = ".claim.json"


class ClaimMetadataError(OSError):
    """The ownership sidecar for a claim could not be written.

    The claim rename that preceded it has no meaning without an owner, so the
    caller that raised into owns the rollback: the markdown goes back to
    `pending/` and the claim is not reported as taken.
    """


@dataclass
class ClaimMetadata:
    """What a claim's sidecar records about ownership."""

    owner: str = OWNER_UNKNOWN
    claimed_at: float = 0.0

    @property
    def is_known(self) -> bool:
        """False when the sidecar was absent, unreadable, or held no owner."""
        return self.owner != OWNER_UNKNOWN


def metadata_path(claim_file: Path) -> Path:
    """The sidecar path that belongs beside `claim_file`."""
    return claim_file.with_name(claim_file.name + _SUFFIX)


def write_metadata(claim_file: Path, owner: str,
                   claimed_at: float | None = None) -> Path:
    """Record `owner` as the holder of `claim_file`, atomically.

    Returns the sidecar path. Raises `ClaimMetadataError` when the write or the
    rename into place fails; the caller rolls back the claim rename.
    """
    dest = metadata_path(claim_file)
    payload = {
        "version": SCHEMA_VERSION,
        "owner": owner,
        "claimed_at": time.time() if claimed_at is None else claimed_at,
        "claim_file": claim_file.name,
    }
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload) + "\n")
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise ClaimMetadataError(
            f"cannot record owner {owner!r} for claim {claim_file.name}: {exc}"
        ) from exc
    return dest


def read_metadata(claim_file: Path) -> ClaimMetadata:
    """The sidecar beside `claim_file`, or `OWNER_UNKNOWN` when there is none.

    An absent file, unreadable bytes, non-JSON, a non-object payload, or a
    missing/blank owner all read back as unknown; a corrupt timestamp reads
    back as 0.0. Nothing here raises — reporting and policy live in the caller.
    """
    try:
        payload = json.loads(metadata_path(claim_file).read_text())
    except (OSError, ValueError):
        return ClaimMetadata()
    if not isinstance(payload, dict):
        return ClaimMetadata()
    owner = payload.get("owner")
    if not isinstance(owner, str) or not owner:
        return ClaimMetadata()
    claimed_at = payload.get("claimed_at")
    if isinstance(claimed_at, bool) or not isinstance(claimed_at, (int, float)):
        claimed_at = 0.0
    return ClaimMetadata(owner=owner, claimed_at=float(claimed_at))


def remove_metadata(claim_file: Path) -> None:
    """Drop the sidecar beside `claim_file` once the claim no longer exists.

    A claim that ended must not leave an owner behind: the next claim taken on
    the same name writes its own sidecar, and a stale one would misattribute it.
    """
    try:
        metadata_path(claim_file).unlink(missing_ok=True)
    except OSError:
        # A leftover sidecar is reported as its own anomaly by the queue audit;
        # it must not fail the claim transition that removed the markdown.
        pass
