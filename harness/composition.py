"""Composition root: builds and wires all dependencies."""
from __future__ import annotations

from pathlib import Path

from .core.config import load
from .core.logsink import LogSink
from .core.providers import create_provider
from .core.session import SessionRunner
from .core.stats import StatsStore
from .core.stand_down import StandDownWatcher
from .core.sync import SyncEngine
from .core.sync_comments import HandoffCommentPoster
from .core.sync_handoff_hook import HandoffSyncHook
from .workflow.demo_generate import (
    DemoAppGenerationHook,
    DemoGenerationHookParams,
)
from .workflow.demo_final_deploy import (
    DemoFinalDeployHook,
    FinalDeployParams,
)
from .workflow.demo_placeholder import (
    DemoPlaceholderHook,
    PlaceholderDeployParams,
)
from .workflow.pipeline import Pipeline
from external.github_api import GitHubApiClient, GitHubApiConfig
from .workflow.task_lifecycle import QUEUE_LOCATIONS_ALL


def build(cfg_path: Path | None = None, repo: str | Path | None = None) -> tuple:
    """Build and wire all dependencies.

    Returns (cfg, store, runner, provider, pipeline, log). The log sink is
    part of the wiring so callers (the CLI handlers) write to the very same
    `work/logs/harness.log` the modules do — passed explicitly, no global.
    `repo`, when passed, sets the target repository for git operations and
    overrides any `repoDir` configured in `config.json`.
    """
    if cfg_path is None:
        import os
        if "HARNESS_CONFIG" in os.environ:
            cfg_path = Path(os.environ["HARNESS_CONFIG"])
        elif Path("config.json").exists():
            cfg_path = Path("config.json").resolve()
        else:
            cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    cfg = load(cfg_path)
    if repo is not None:
        cfg.repo_dir = Path(repo).expanduser().resolve()
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    for sub in QUEUE_LOCATIONS_ALL:
        (cfg.queue_dir / sub).mkdir(parents=True, exist_ok=True)
    log = LogSink(cfg.logs_dir / "harness.log")
    store = StatsStore(cfg.stats_path)
    runner = SessionRunner(cfg, store, log=log)
    provider = create_provider(cfg)
    stand_down = StandDownWatcher(getattr(cfg, "work_dir", None), log=log)
    handoff_sync = None
    sync_engine = None
    if cfg.github_sync_enabled:
        # Spec FR-2.5/FR-3: one poster and one engine shared by the
        # pipeline and its lifecycle through the handoff hook; with GitHub
        # unconfigured both stay None and every sync site is a no-op
        # (FR-0.1, NFR-2).
        api = build_github_api(cfg, log=log)
        poster = HandoffCommentPoster(api, cfg.queue_dir,
                                      cfg.github_repo, log=log)
        # Spec FR-3: the same engine instance goes to the pipeline and its
        # lifecycle, so hook sites share one dispatcher (no global).
        sync_engine = SyncEngine(cfg, api, log=log, comment_poster=poster)
        # Spec FR-3 in-flight rule: the handoff write sites post through
        # the poster *and* run the targeted + inbound pass through this
        # hook, which shares the engine's own poster (one dedup map).
        handoff_sync = HandoffSyncHook(sync_engine, poster, log=log)
    placeholder_hook = None
    if sync_engine is not None:
        # Demo spec FR-6.2: the placeholder hook is wired only when the
        # feature is enabled and GitHub is configured; otherwise it stays
        # None and the pipeline hook site is a no-op.
        placeholder_hook = build_placeholder_hook(cfg, api=api, log=log)
    demo_app_generator = None
    if sync_engine is not None:
        # Demo spec FR-3: the app generation driver is wired under the
        # same gate as the placeholder hook — feature enabled and GitHub
        # configured — and stays None (implement stage unchanged) else.
        demo_app_generator = build_demo_app_generator(cfg, api=api, log=log)
    final_deploy_hook = None
    if sync_engine is not None:
        # Demo spec FR-6.2 (final half): the post-merge Pages deployment
        # hook, wired under the same gate as the placeholder hook.
        final_deploy_hook = build_final_deploy_hook(cfg, api=api, log=log)
    pipeline = Pipeline(cfg, runner, log=log, provider=provider,
                        stand_down_check=stand_down,
                        handoff_sync=handoff_sync,
                        sync_engine=sync_engine,
                        placeholder_hook=placeholder_hook,
                        demo_app_generator=demo_app_generator,
                        final_deploy_hook=final_deploy_hook)
    return cfg, store, runner, provider, pipeline, log


def build_sync_engine(cfg, log=None, api=None) -> SyncEngine | None:
    """Build the sync dispatcher (spec FR-3) from config.

    Returns None when GitHub is unconfigured — the feature is inert and no
    client is built, so a hook site holding this can never make an HTTP
    call (FR-0.1, NFR-2). `api` and `log` are injectable so tests drive the
    engine through the same composition entry the CLI uses; passing an api
    skips client construction entirely.
    """
    if api is None and not cfg.github_sync_enabled:
        return None
    if log is None:
        log = LogSink(cfg.logs_dir / "harness.log")
    if api is None:
        api = build_github_api(cfg, log=log)
    poster = HandoffCommentPoster(api, cfg.queue_dir, cfg.github_repo, log=log)
    return SyncEngine(cfg, api, log=log, comment_poster=poster)


def build_handoff_sync(engine, log=None):
    """Wrap a dispatcher in the handoff write-site hook (spec FR-3).

    Returns None when there is no engine — GitHub unconfigured — which
    keeps every handoff write site a no-op (FR-0.1, NFR-2). The poster is
    the engine's own, so comment dedup and the failed-post retry queue
    stay shared with the targeted pass the hook runs."""
    if engine is None:
        return None
    return HandoffSyncHook(engine, engine.comment_poster,
                           log=log if log is not None else engine.log)


def build_placeholder_hook(cfg, api=None, log=None) -> DemoPlaceholderHook | None:
    """Build the pre-spec placeholder deploy hook (demo spec FR-2, FR-6.2).

    Returns None when the demo feature is disabled or GitHub is
    unconfigured — the pipeline hook site then never fires, and no HTTP
    or git call is reachable through it. `api` and `log` are injectable
    so tests drive the hook through the same composition entry the
    runtime uses (the `build_github_api` convention).
    """
    if not cfg.demo.enabled:
        return None
    if api is None and not cfg.github_sync_enabled:
        return None
    if log is None:
        log = LogSink(cfg.logs_dir / "harness.log")
    if api is None:
        api = build_github_api(cfg, log=log)
    params = PlaceholderDeployParams(
        queue_dir=cfg.queue_dir,
        apps_dir=cfg.demo.apps_dir,
        harness_repo=cfg.repo_dir,
        deploy_dir=cfg.demo.deploy_dir,
        deploy_branch=cfg.demo.deploy_branch,
        trunk_branch=cfg.trunk_branch,
        docs_dir=cfg.demo.docs_dir)
    return DemoPlaceholderHook(params, api, log=log)


def build_demo_app_generator(cfg, api=None, log=None) -> DemoAppGenerationHook | None:
    """Build the implement-stage app generation driver (demo spec FR-3).

    Returns None when the demo feature is disabled or GitHub is
    unconfigured — the pipeline then never runs a generation session and
    a demo task falls back to the plain implementer flow with the demo
    prompt appendix. Follows the `build_placeholder_hook` convention for
    injectable `api`/`log`.
    """
    if not cfg.demo.enabled:
        return None
    if api is None and not cfg.github_sync_enabled:
        return None
    if log is None:
        log = LogSink(cfg.logs_dir / "harness.log")
    if api is None:
        api = build_github_api(cfg, log=log)
    params = DemoGenerationHookParams(
        queue_dir=cfg.queue_dir,
        apps_dir=cfg.demo.apps_dir,
        repo=cfg.github_repo,
        content_model=cfg.demo.content_model,
        fallback_topic=cfg.demo.fallback_topic,
        app_model=cfg.implementer,
        output_dir=cfg.logs_dir / "demo-generation")
    return DemoAppGenerationHook(params, api, log=log)


def build_final_deploy_hook(cfg, api=None,
                            log=None) -> DemoFinalDeployHook | None:
    """Build the post-merge final deploy hook (demo spec FR-6.2, FR-7).

    Returns None when the demo feature is disabled or GitHub is
    unconfigured — the pipeline hook site then never fires, and no HTTP,
    npm or git call is reachable through it. Follows the
    `build_placeholder_hook` convention for injectable `api`/`log`.
    """
    if not cfg.demo.enabled:
        return None
    if api is None and not cfg.github_sync_enabled:
        return None
    if log is None:
        log = LogSink(cfg.logs_dir / "harness.log")
    if api is None:
        api = build_github_api(cfg, log=log)
    params = FinalDeployParams(
        queue_dir=cfg.queue_dir,
        apps_dir=cfg.demo.apps_dir,
        harness_repo=cfg.repo_dir,
        deploy_dir=cfg.demo.deploy_dir,
        deploy_branch=cfg.demo.deploy_branch,
        trunk_branch=cfg.trunk_branch,
        docs_dir=cfg.demo.docs_dir)
    return DemoFinalDeployHook(params, api, log=log)


def build_github_api(cfg, log=None) -> GitHubApiClient:
    """Build the GitHub REST client from config (spec FR-5).

    The composition root is the only place that turns config keys into the
    client's parameters object. Callers check `cfg.github_sync_enabled`
    first — this factory does not re-check it (FR-0.1 is the callers' gate,
    so a disabled build is a programming error, not a silent no-op).
    """
    return GitHubApiClient(
        GitHubApiConfig(pat=cfg.github_pat,
                        repo=cfg.github_repo,
                        base_url=cfg.github_api_base_url),
        log=log)