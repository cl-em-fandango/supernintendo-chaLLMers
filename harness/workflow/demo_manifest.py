"""The active-app manifest: `demo-apps/DEPLOYED.json` (demo spec FR-7.1).

One demo deployment always publishes exactly one app — the *active* app.
Which app that is lives in a small JSON document beside the app sources:

    {"app": "<app-name>", "issue": <number>, "task": "<task-id>"}

It is written by the generation hook into the task workdir (so it flows
to trunk with the app source through the normal merge) and read by the
final-deploy hook, which builds exactly the named app and never an
arbitrary scan of `demo-apps/` (FR-7.2: older app directories remain as
history, unbuilt and unserved).

This module owns the manifest's shape and its reads/writes; the deploy
and build modules consume it. Writes are atomic (temp file + rename), so
a crash can never leave a half-written manifest behind.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# The manifest file name inside the apps directory (FR-7.1).
MANIFEST_NAME = "DEPLOYED.json"

# An app name is a single safe kebab-case path segment — the same shape
# `demo_scaffold.validate_app_name` writes. Separators and traversal are
# rejected before any path is built from the manifest.
_APP_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


class ManifestError(ValueError):
    """A manifest could not be read, or a write was refused as unsafe."""


@dataclass(frozen=True)
class ActiveAppManifest:
    """Which app is the active deployment (FR-7.1), as named fields."""

    app: str
    issue: int
    task: str


def manifest_path(apps_dir: Path) -> Path:
    """The manifest file inside an apps directory."""
    return Path(apps_dir) / MANIFEST_NAME


def write_manifest(apps_dir: Path, manifest: ActiveAppManifest) -> Path:
    """Atomically write the active-app manifest; return its path.

    Raises `ManifestError` when a field is unusable (empty app/task, an
    app name that is not a single safe path segment, a non-integer
    issue) — a bad manifest must fail here, not at deploy time.
    """
    app = str(manifest.app or "")
    if not _APP_NAME_RE.fullmatch(app):
        raise ManifestError(f"unsafe app name in manifest: {app!r}")
    task = str(manifest.task or "")
    if not task:
        raise ManifestError("manifest task id must not be empty")
    payload = {"app": app, "issue": int(manifest.issue), "task": task}
    target = manifest_path(apps_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_manifest(apps_dir: Path) -> ActiveAppManifest | None:
    """Read the manifest, or None when it is absent.

    Raises `ManifestError` when a file exists but is corrupt or carries
    unusable fields: an unreadable manifest is a deployment-blocking
    condition (which app to build would be a guess), unlike a missing
    one (nothing has been deployed yet).
    """
    target = manifest_path(apps_dir)
    if not target.is_file():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"manifest {target} is unreadable: {exc}")
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest {target} is not a JSON object")
    app = str(raw.get("app") or "")
    task = str(raw.get("task") or "")
    if not _APP_NAME_RE.fullmatch(app):
        raise ManifestError(f"manifest {target} has an unsafe app name: "
                            f"{app!r}")
    if not task:
        raise ManifestError(f"manifest {target} has an empty task id")
    try:
        issue = int(raw.get("issue"))
    except (TypeError, ValueError):
        raise ManifestError(f"manifest {target} has a non-integer issue")
    return ActiveAppManifest(app=app, issue=issue, task=task)
