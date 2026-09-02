"""Build runner for the active demo app (demo spec FR-7.3).

At deploy time the stack is already fixed — the generated project
declares its own toolchain — so this module reads the *app directory*
rather than re-detecting anything:

  * a `package.json` means a node project: `npm install` then
    `npm run build`, artifacts in the directory the *declared* build
    tool writes to (`vite build` -> `dist/`, `react-scripts build`
    -> `build/`) — never a hardcoded stack constant (FR-7.3);
  * a bare `index.html` is the no-build static app (FR-2.2): the app
    directory itself is the artifact tree;
  * anything else is not a deployable app.

All npm calls go through the `external/npm_cli` boundary with fixed argv
fragments — no shell, and nothing from the app or the model ever becomes
a command (spec §6). A missing npm or a failing build raises
`AppBuildError` with a short reason; there is no silent stack swap.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from external.npm_cli import NpmResult, npm_available, run_npm

# Standard artifact directories per build tool (FR-7.3); the scaffold
# that produced the app builds into the same places.
CRA_ARTIFACT_DIR = "build"
VUE_ARTIFACT_DIR = "dist"

# The project's own `scripts.build` names its tool; each tool's default
# output directory is what the deploy must publish. Detection order is
# irrelevant — a scaffolded project declares exactly one of them.
_TOOL_ARTIFACT_DIRS: tuple[tuple[str, str], ...] = (
    ("vite", VUE_ARTIFACT_DIR),
    ("react-scripts", CRA_ARTIFACT_DIR),
)

# Reason fragment when the deploy host has no npm (spec §6).
NPM_UNAVAILABLE_REASON = ("npm unavailable: Node.js/npm is required to "
                          "build the app and was not found on PATH")


class AppBuildError(RuntimeError):
    """The active app could not be built; the message is the FR-8 reason."""


@dataclass(frozen=True)
class AppBuildPlan:
    """How one already-generated app declares itself built.

    `build_commands` are npm argv tuples run in the app directory in
    order (empty for the no-build static app); `artifact_dir` is
    relative to the app directory (`.` for the static app).
    """

    build_commands: tuple[tuple[str, ...], ...]
    artifact_dir: str


def build_plan_for(app_dir: Path) -> AppBuildPlan:
    """The build plan the app directory declares (FR-7.3).

    Raises `AppBuildError` when the directory declares no app at all.
    """
    app_dir = Path(app_dir)
    package_json = app_dir / "package.json"
    if package_json.is_file():
        return AppBuildPlan(
            build_commands=(("install",), ("run", "build")),
            artifact_dir=_declared_artifact_dir(package_json))
    if (app_dir / "index.html").is_file():
        return AppBuildPlan(build_commands=(), artifact_dir=".")
    raise AppBuildError(
        f"{app_dir} declares no build: no package.json or index.html")


def build_active_app(app_dir: Path, *,
                     npm_probe: Callable[[], bool] = npm_available,
                     npm_runner: Callable[..., NpmResult] = run_npm,
                     log: Callable[[str], None] = lambda message: None,
                     ) -> Path:
    """Build the app in `app_dir`; return its artifact directory.

    Runs the app's own build commands in the app directory through the
    npm boundary and verifies the declared artifact directory holds an
    `index.html`. Raises `AppBuildError` on a missing npm, a failing
    command, or a build that produced nothing.
    """
    app_dir = Path(app_dir)
    plan = build_plan_for(app_dir)
    if not plan.build_commands:
        return (app_dir / plan.artifact_dir).resolve()
    if not npm_probe():
        raise AppBuildError(NPM_UNAVAILABLE_REASON)
    for command in plan.build_commands:
        result = npm_runner(command, cwd=app_dir)
        if result.rc != 0:
            raise AppBuildError(
                f"npm {' '.join(command)} failed (rc={result.rc}): "
                f"{_tail(result.stderr or result.stdout)}")
    artifacts = app_dir / plan.artifact_dir
    if not (artifacts / "index.html").is_file():
        raise AppBuildError(
            f"build produced no {plan.artifact_dir}/index.html in {app_dir}")
    log(f"  demo build: {app_dir.name} -> {artifacts}")
    return artifacts


def _declared_artifact_dir(package_json: Path) -> str:
    """The output directory of the build tool `scripts.build` declares.

    The app's own package.json is the authority (FR-7.3): a vite project
    publishes `dist/`, a create-react-app project publishes `build/`.
    Raises `AppBuildError` on unreadable JSON or a build script naming
    no known tool — the deploy never guesses an artifact directory.
    """
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
        script = str(data.get("scripts", {}).get("build", ""))
    except (OSError, ValueError) as exc:
        raise AppBuildError(
            f"{package_json} is not readable JSON: {exc}") from exc
    for tool, artifact_dir in _TOOL_ARTIFACT_DIRS:
        if tool in script:
            return artifact_dir
    raise AppBuildError(
        f"{package_json} build script {script!r} names no known build "
        f"tool ({', '.join(tool for tool, _ in _TOOL_ARTIFACT_DIRS)})")


def _tail(text: str, limit: int = 200) -> str:
    clean = (text or "").strip()
    return clean[-limit:] if len(clean) > limit else clean
