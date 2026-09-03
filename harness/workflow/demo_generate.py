"""Demo app generation driver (FR-3, FR-4.3).

Composes the demo generation steps for one claimed demo task into a
single, testable call:

  1. pick the stack — the ticket's explicit request wins, else the
     create-react-app + Material UI default (`demo_stack`);
  2. write the deterministic scaffold with the Slice 6 content baked in
     as `content.json` (`demo_scaffold`), all writes confined to
     `demo-apps/<app-name>/`;
  3. run the one-shot generation session so a model fleshes the app out
     inside the app directory (`external/pi_cli.run_pi_session`);
  4. run the stack's declared build through the npm boundary
     (`external/npm_cli`) and verify the standard artifact directory.

The outcome is a `GenerationOutcome` that says whether the app is built
and, when it is not, a short reason. A missing npm is reported as such —
the stack was fixed at generation time and is never silently swapped
(spec §6). Nothing here talks to GitHub or git; the deployment slices
consume the outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from external.demo_deploy import DeployStep
from external.npm_cli import NpmResult, npm_available, run_npm
from external.pi_cli import run_pi_session

from ..core import prompts
from ..core import task_record
from .demo_app_name import resolve_generation_app_name
from .demo_content import (
    ContentGenerationParams,
    ContentRequest,
    SiteContent,
    generate_content,
)
from .demo_final_deploy import DEPLOY_FAILURE_COMMENT
from .demo_manifest import ActiveAppManifest, write_manifest
from .demo_placeholder import placeholder_issue_of
from .demo_scaffold import scaffold_app
from .demo_stack import StackPlan, build_stack_plan, detect_stack

# Reason fragment reported when the build environment lacks npm (§6).
NPM_UNAVAILABLE_REASON = ("npm unavailable: Node.js/npm is required to "
                          "build the {stack} app and was not found on PATH")


@dataclass(frozen=True)
class DemoGenerationParams:
    """Everything the driver needs, as one parameters object (§5).

    `apps_dir` is the repo-relative apps directory (`demo.appsDir`),
    `repo` the `owner/repo` slug the Pages base path is derived from,
    `app_model` the model for the generation session, `output_dir` where
    the session's raw output file is written.
    """

    apps_dir: str
    repo: str
    app_model: str
    output_dir: Path


@dataclass(frozen=True)
class DemoGenerationRequest:
    """One demo task's generation input: ticket text, app name, content.

    `app_name` is resolved by the caller
    (`demo_app_name.resolve_generation_app_name` — the final app
    replaces the placeholder in the same directory, FR-2.4).
    """

    title: str
    body: str
    app_name: str
    content: SiteContent


@dataclass(frozen=True)
class GenerationOutcome:
    """What one generation run produced, and whether it built."""

    app_dir: Path
    plan: StackPlan
    built: bool
    reason: str


def generate_demo_app(
    params: DemoGenerationParams,
    request: DemoGenerationRequest,
    workdir: Path,
    *,
    session_runner: Callable = run_pi_session,
    npm_probe: Callable[[], bool] = npm_available,
    npm_runner: Callable[..., NpmResult] = run_npm,
    log: Callable[[str], None] = lambda message: None,
) -> GenerationOutcome:
    """Scaffold, generate and build one demo app; never raises on build
    environment problems — the outcome carries the reason instead."""
    stack = detect_stack(f"{request.title}\n{request.body}")
    plan = build_stack_plan(stack, params.repo)
    app_dir = scaffold_app(Path(workdir) / params.apps_dir,
                           request.app_name, plan, request.content)
    log(f"  demo generate: {request.app_name} -> stack {stack.value}, "
        f"public path {plan.public_path}")

    _run_generation_session(params, plan, app_dir, Path(workdir),
                            session_runner, log)

    if not plan.needs_build:
        built = (app_dir / "index.html").exists()
        reason = ("" if built
                  else "static app has no index.html after generation")
        return GenerationOutcome(app_dir=app_dir, plan=plan, built=built,
                                 reason=reason)

    if not npm_probe():
        return GenerationOutcome(
            app_dir=app_dir, plan=plan, built=False,
            reason=NPM_UNAVAILABLE_REASON.format(stack=plan.stack.value))

    for command in plan.build_commands:
        result = npm_runner(command, cwd=app_dir)
        if result.rc != 0:
            return GenerationOutcome(
                app_dir=app_dir, plan=plan, built=False,
                reason=(f"npm {' '.join(command)} failed (rc="
                        f"{result.rc}): {_tail(result.stderr)}"))

    artifact_index = app_dir / plan.artifact_dir / "index.html"
    if not artifact_index.exists():
        return GenerationOutcome(
            app_dir=app_dir, plan=plan, built=False,
            reason=(f"build produced no {plan.artifact_dir}/index.html "
                    f"in {app_dir}"))
    return GenerationOutcome(app_dir=app_dir, plan=plan, built=True,
                             reason="")


# --- internals ----------------------------------------------------------

def _run_generation_session(params: DemoGenerationParams, plan: StackPlan,
                            app_dir: Path, workdir: Path,
                            session_runner: Callable,
                            log: Callable[[str], None]) -> None:
    """One generation session over the scaffold; a dead model is logged,
    not fatal — the scaffold alone is already a buildable app."""
    prompt = prompts.demo_app_generation(
        app_dir=app_dir, stack_label=plan.stack.value,
        public_path=plan.public_path)
    try:
        result = session_runner(
            model=params.app_model,
            workdir=workdir,
            prompt=prompt,
            out_file=Path(params.output_dir) / "demo-generate.out",
            log=log)
    except Exception as exc:  # noqa: BLE001 - a dead model keeps the scaffold
        log(f"  demo generate: generation session raised: {exc}; "
            f"keeping the scaffold")
        return
    if result.crashed or result.rc != 0:
        log(f"  demo generate: generation session failed (rc="
            f"{result.rc}, crashed={result.crashed}); keeping the "
            f"scaffold")


@dataclass(frozen=True)
class DemoGenerationHookParams:
    """Parameters for `DemoAppGenerationHook`, built by the composition
    root from `Config` + `DemoConfig` (§5). `queue_dir` locates the
    task's GitHub linkage sidecar; `repo` is the `owner/repo` slug the
    Pages base path derives from."""

    queue_dir: Path
    apps_dir: str
    repo: str
    content_model: str
    fallback_topic: str
    app_model: str
    output_dir: Path


class DemoAppGenerationHook:
    """The pipeline's `demo_app_generator`: ticket text -> content ->
    scaffolded, generated, built app directory.

    Called as `hook(ctx)` before the first implement session of a demo
    task. Reads the ticket from the task's `original.md`, the issue
    title through the injected GitHub client, generates the site content
    (FR-4) and runs `generate_demo_app`. The app directory is resolved
    with `resolve_generation_app_name`: this issue's placeholder
    directory is reused so the final app replaces the placeholder in
    place (FR-2.4), and only a bare name owned by another app forces the
    `-<issue>` suffix (edge case 8). Failures
    propagate to the pipeline's guard (the failure-handling slice owns
    comments and routing).
    """

    def __init__(self, params: DemoGenerationHookParams, api, *,
                 content_generator: Callable = generate_content,
                 generator: Callable = generate_demo_app,
                 log: Callable[[str], None] = print):
        self.params = params
        self.api = api
        self.content_generator = content_generator
        self.generator = generator
        self.log = log

    def __call__(self, ctx) -> GenerationOutcome | None:
        linkage = self._linkage(ctx.task_id)
        if linkage is None:
            self.log(f"  demo generate: task {ctx.task_id} has no GitHub "
                     f"linkage; nothing generated")
            return None
        issue = linkage.issue
        try:
            body = self._ticket_text(ctx)
            title = self.api.get_issue(issue).title
            app_name = resolve_generation_app_name(
                title, issue,
                Path(ctx.workdir) / self.params.apps_dir,
                placeholder_issue_of)
            content = self.content_generator(
                ContentGenerationParams(
                    content_model=self.params.content_model,
                    fallback_topic=self.params.fallback_topic,
                    workdir=Path(ctx.workdir),
                    output_dir=Path(self.params.output_dir)),
                ContentRequest(title=title, body=body),
                log=self.log)
        except Exception as exc:  # noqa: BLE001 - FR-8.1 step-named comment
            self._comment_failure(issue, DeployStep.CONTENT, exc)
            raise
        gen_params = DemoGenerationParams(
            apps_dir=self.params.apps_dir,
            repo=linkage.repo or self.params.repo,
            app_model=self.params.app_model,
            output_dir=Path(self.params.output_dir))
        request = DemoGenerationRequest(title=title, body=body,
                                        app_name=app_name, content=content)
        try:
            outcome = self.generator(gen_params, request, Path(ctx.workdir),
                                     log=self.log)
        except Exception as exc:  # noqa: BLE001 - FR-8.1 step-named comment
            self._comment_failure(issue, DeployStep.SCAFFOLD, exc)
            raise
        self._mark_active(ctx, app_name, issue)
        return outcome

    # --- internals ----------------------------------------------------

    def _comment_failure(self, issue: int, step: DeployStep,
                         exc: Exception) -> None:
        """Post the FR-8.1 comment naming the failed generation step, then
        let the exception propagate to the pipeline's guard (the task
        continues into the implementer; only the *final* deploy routes to
        `failed/`). A failing comment dies here — it never replaces the
        original failure."""
        self.log(f"  ⚠ demo generate failed at {step.value}: {exc}")
        try:
            self.api.create_comment(issue, DEPLOY_FAILURE_COMMENT.format(
                step=step.value, reason=exc))
        except Exception as comment_exc:  # noqa: BLE001
            self.log(f"  ⚠ generation failure comment not posted: "
                     f"{comment_exc}")

    def _mark_active(self, ctx, app_name: str, issue: int) -> None:
        """Record the generated app as the active one (FR-7.1).

        The manifest is written beside the app source in the task
        workdir, so it flows to trunk with the app through the normal
        merge and the final-deploy hook reads trunk's verdict — never a
        scan of the apps directory. It is written whatever the build
        outcome was: the implementer sessions may still fix the build,
        and the final deploy rebuilds from trunk anyway. A manifest
        write failure is logged, not fatal — the failure-handling slice
        owns deployment failure routing.
        """
        try:
            write_manifest(Path(ctx.workdir) / self.params.apps_dir,
                           ActiveAppManifest(app=app_name, issue=issue,
                                             task=ctx.task_id))
        except Exception as exc:  # noqa: BLE001 - generation continues
            self.log(f"  ⚠ demo generate: could not write the active-app "
                     f"manifest: {exc}")

    def _linkage(self, task_id: str):
        """The task's linkage, resolved by task id (FR-1.4)."""
        return task_record.read_linkage(self.params.queue_dir, task_id)

    @staticmethod
    def _ticket_text(ctx) -> str:
        original = Path(ctx.task_dir) / "original.md"
        return original.read_text(encoding="utf-8") if original.exists() \
            else ""


def _tail(text: str, limit: int = 200) -> str:
    clean = (text or "").strip()
    return clean[-limit:] if len(clean) > limit else clean
