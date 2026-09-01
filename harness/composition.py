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
    pipeline = Pipeline(cfg, runner, log=log, provider=provider,
                        stand_down_check=stand_down,
                        handoff_sync=handoff_sync,
                        sync_engine=sync_engine)
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