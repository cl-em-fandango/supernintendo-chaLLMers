"""Claim ownership: the shape of one task's `claim` metadata section.

A claim is recorded twice. The markdown file itself is the claim (it was moved
out of `pending/`), and the task's metadata record (`task_record.py`) says
which invocation took it and when. Without that ownership, a cleanup path
cannot tell its own claim from one held by another live invocation, so it
either steals work or leaves its own behind.

This module is the *shape* only: `ClaimMetadata` is the `claim` section of the
task record, and nothing here derives a path. Ownership used to live in a
`claimed/<name>.md.claim.json` sidecar beside the claim; that derivation is
gone with the record, and the legacy name is known only to the migration
reader in `task_record`.

Missing or unreadable ownership is not an error at this layer: it reads back as
`OWNER_UNKNOWN` so a caller can report it and decide what to do.
"""
from __future__ import annotations

from dataclasses import dataclass

OWNER_UNKNOWN = "unknown"      # owner of a claim whose record is absent or corrupt


class ClaimMetadataError(OSError):
    """The ownership section of a claim's record could not be written.

    The claim rename that preceded it has no meaning without an owner, so the
    caller that raised into owns the rollback: the markdown goes back to
    `pending/` and the claim is not reported as taken.
    """


@dataclass
class ClaimMetadata:
    """What a task's record holds about claim ownership."""

    owner: str = OWNER_UNKNOWN
    claimed_at: float = 0.0

    @property
    def is_known(self) -> bool:
        """False when the record was absent, unreadable, or held no owner."""
        return self.owner != OWNER_UNKNOWN
