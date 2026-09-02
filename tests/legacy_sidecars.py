"""Seeding the pre-record metadata formats — test support, not production.

A queue written before `<queue>/.meta/<task-id>.json` carried one sidecar per
concern, named after the task *file*: `X.md.gh.json` beside a task file,
`gh.json` inside a task directory, and `X.md.claim.json` beside a claim. The
production code no longer derives, writes or reads those paths from anywhere
but the migration reader (`harness/core/task_record.py`), so a test that wants
to prove backward compatibility (FR-E1/FR-E2) builds the old shape here.

The payloads are the exact old shapes: the linkage writer keeps the pre-demo
sidecar byte-for-byte (the `demo` key appears only on flagged tasks), and the
claim writer keeps the `version`/`claim_file` keys the old
`claim_metadata.write_metadata` wrote, so a migrated queue is a real one.
"""
from __future__ import annotations

import json
from pathlib import Path

from harness.core.sync_linkage import SyncLinkage
from harness.core.sync_sidecar import (
    SIDECAR_SUFFIX,
    TASK_DIR_SIDECAR_NAME,
)
from harness.workflow.task_lifecycle import write_atomic

CLAIM_SIDECAR_SUFFIX = ".claim.json"


def file_sidecar_path(task_file: Path) -> Path:
    """The legacy linkage sidecar path for a task *file*."""
    return task_file.with_name(task_file.name + SIDECAR_SUFFIX)


def task_dir_sidecar_path(task_dir: Path) -> Path:
    """The legacy linkage sidecar path inside a task directory."""
    return task_dir / TASK_DIR_SIDECAR_NAME


def claim_sidecar_path(claim_file: Path) -> Path:
    """The legacy claim-ownership sidecar path beside `claim_file`."""
    return claim_file.with_name(claim_file.name + CLAIM_SIDECAR_SUFFIX)


def write_legacy_linkage(sidecar: Path, linkage: SyncLinkage) -> None:
    """Atomically write `linkage` to a legacy `sidecar` path."""
    payload = {
        "issue": linkage.issue,
        "repo": linkage.repo,
        "comment_ids": dict(linkage.comment_ids),
    }
    if linkage.demo:
        payload["demo"] = True
    write_atomic(sidecar, json.dumps(payload, indent=2) + "\n")


def write_legacy_claim(claim_file: Path, owner: str,
                       claimed_at: float) -> Path:
    """Write the legacy `X.md.claim.json` beside `claim_file`."""
    dest = claim_sidecar_path(claim_file)
    write_atomic(dest, json.dumps({
        "version": 1,
        "owner": owner,
        "claimed_at": claimed_at,
        "claim_file": claim_file.name,
    }) + "\n")
    return dest
