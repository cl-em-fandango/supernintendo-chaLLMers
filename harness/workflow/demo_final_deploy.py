"""Final demo deployment: publish the active app after merge-to-trunk.

Demo spec FR-6.2 (final half) and FR-7: once a demo task's app source has
merged to trunk, the app becomes the *active* app — the one named in the
`demo-apps/DEPLOYED.json` manifest — and is the only app built and
published to `docs/` on the deploy branch. Older app directories stay in
the repository as history and are never built (FR-7.2).

`DemoFinalDeployHook` is the callable the pipeline invokes inside
`stage_holistic`, after `merge_to_trunk` succeeded and before the
completion move, so a failure here can still route the task to `failed/`
(FR-8.1). The build runs inside the deploy checkout (FR-7.3): the hook
hands the Slice 4 deployer a builder closure that builds the
manifest-named app in the freshly rebased trunk tree, and the deployer
publishes the resulting artifacts as the sole content of `docs/`.

Contract with the pipeline: the hook returns `""` on success or
deliberate skip, and a short failure reason otherwise — it comments on
the issue itself (FR-8.2) and never raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from external.demo_deploy import (
    DemoDeployError,
    DemoDeployRequest,
    origin_url_from_repo,
    publish_artifacts,
)

from ..core.sync_sidecar import resolve_linkage
from .demo_build import build_active_app
from .demo_manifest import MANIFEST_NAME, read_manifest
from .demo_pages_url import pages_url

# FR-8.2 success comment; FR-8.1 failure comment (`step` names the step).
DEPLOY_SUCCESS_COMMENT = "Deployed: {url}"
DEPLOY_FAILURE_COMMENT = "Demo deployment failed at {step}: {reason}"

# Step name for failures that happen before the deployer's own steps
# (missing linkage data, missing manifest, missing app directory).
PREP_STEP = "prepare"


@dataclass(frozen=True)
class FinalDeployParams:
    """Everything the final-deploy hook needs, as one parameters object.

    Built by the composition root from `Config` + `DemoConfig`; the hook
    holds no globals (CODING_STANDARDS §5). `harness_repo` is the harness
    workdir whose local trunk the deployer refreshes from (FR-5.2.b).
    """

    queue_dir: Path
    apps_dir: str
    harness_repo: Path
    deploy_dir: Path
    deploy_branch: str
    trunk_branch: str
    docs_dir: str


class DemoFinalDeployHook:
    """Post-merge hook: build the manifest-named app, publish `docs/`,
    comment the Pages URL (FR-6.2, FR-7, FR-8.2).

    Called as `hook(ctx)` with the pipeline's `StageContext`; the task's
    GitHub linkage is read from its task-directory sidecar, the active
    app from the manifest in the (now-merged) workdir. `deployer`,
    `builder` and `origin_resolver` are injectable so tests drive the
    hook with a fake publication and a fake origin; the defaults are the
    real Slice 4 deployer, the real build runner and the harness repo's
    own `origin`.
    """

    def __init__(self, params: FinalDeployParams, api, *,
                 deployer=publish_artifacts,
                 builder: Callable[..., Path] = build_active_app,
                 origin_resolver=origin_url_from_repo, log=print):
        self.params = params
        self.api = api
        self.deployer = deployer
        self.builder = builder
        self.origin_resolver = origin_resolver
        self.log = log

    def __call__(self, ctx) -> str:
        """Deploy the active app for one merged demo task.

        Returns `""` on success or skip, a failure reason otherwise (the
        pipeline routes a non-empty reason to `failed/`). Never raises.
        """
        issue: int | None = None
        try:
            linkage = self._linkage(ctx.task_id)
            if linkage is None:
                self.log(f"  ⚠ final deploy: task {ctx.task_id} has no "
                         f"GitHub linkage; nothing deployed")
                return ""
            issue = linkage.issue
            app_name = self._active_app(ctx)
            self._deploy(app_name)
            self.api.create_comment(issue, DEPLOY_SUCCESS_COMMENT.format(
                url=pages_url(linkage.repo)))
            return ""
        except Exception as exc:  # noqa: BLE001 - pipeline routes the failure
            self.log(f"  ⚠ demo deployment failed: {exc}")
            self._comment_failure(issue, exc)
            return (exc.message if isinstance(exc, DemoDeployError)
                    else str(exc))

    # --- internals ----------------------------------------------------

    def _linkage(self, task_id: str):
        """The task's GitHub linkage, wherever its sidecar currently is.

        The hook fires while the task is still active (before the
        completion move), so the sidecar may live beside the staged
        claim file rather than in the task dir — `resolve_linkage`
        checks both shapes (FR-1.4)."""
        return resolve_linkage(self.params.queue_dir, task_id)

    def _active_app(self, ctx) -> str:
        """The manifest-named active app, verified present in the workdir.

        The hook runs right after `merge_to_trunk`, so the workdir tree
        is the merged result; reading the manifest from `ctx.workdir`
        therefore yields the same manifest trunk carries (the generation
        hook wrote it beside the app source). The app is chosen by the
        manifest, never by a scan of `demo-apps/` (FR-7.1).
        """
        apps_root = Path(ctx.workdir) / self.params.apps_dir
        manifest = read_manifest(apps_root)
        if manifest is None:
            raise RuntimeError(
                f"no {MANIFEST_NAME} on trunk: nothing is the active app")
        app_dir = apps_root / manifest.app
        if not app_dir.is_dir():
            raise RuntimeError(
                f"manifest names app {manifest.app!r} but {app_dir} does "
                f"not exist")
        return manifest.app

    def _deploy(self, app_name: str) -> None:
        """Publish the active app through the deployer (FR-5 sequence).

        The builder closure runs inside the deploy checkout after the
        rebase, so the build sees the trunk tree the rebase just pulled
        in (FR-7.3) and the artifacts are the app's own build output,
        not anything from the harness workdir.
        """
        harness_repo = Path(self.params.harness_repo)

        def build_in_checkout(checkout: Path) -> Path:
            app_dir = Path(checkout) / self.params.apps_dir / app_name
            return self.builder(app_dir, log=self.log)

        self.deployer(DemoDeployRequest(
            harness_repo=harness_repo,
            deploy_dir=Path(self.params.deploy_dir),
            origin_url=self.origin_resolver(harness_repo),
            deploy_branch=self.params.deploy_branch,
            trunk_branch=self.params.trunk_branch,
            docs_dir=self.params.docs_dir,
            builder=build_in_checkout,
            log=self.log))

    def _comment_failure(self, issue: int | None, exc: Exception) -> None:
        """Post the FR-8.1 comment naming the step; a failing comment
        dies here too — the pipeline still routes the task to failed/."""
        if issue is None:
            return
        step = (exc.step.value if isinstance(exc, DemoDeployError)
                else PREP_STEP)
        reason = exc.message if isinstance(exc, DemoDeployError) else str(exc)
        try:
            self.api.create_comment(issue, DEPLOY_FAILURE_COMMENT.format(
                step=step, reason=reason))
        except Exception as comment_exc:  # noqa: BLE001
            self.log(f"  ⚠ deployment failure comment not posted: "
                     f"{comment_exc}")
