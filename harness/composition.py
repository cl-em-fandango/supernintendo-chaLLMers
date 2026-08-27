"""Composition root: builds and wires all dependencies."""
from __future__ import annotations

from pathlib import Path

from .core.config import load
from .core.providers import create_provider
from .core.session import SessionRunner
from .core.stats import StatsStore
from .workflow.pipeline import Pipeline


def _log(line: str = "") -> None:
    print(line, flush=True)


def build(cfg_path: Path | None = None) -> tuple:
    """Build and wire all dependencies. Returns (cfg, store, runner, provider, pipeline)."""
    if cfg_path is None:
        import os
        if "HARNESS_CONFIG" in os.environ:
            cfg_path = Path(os.environ["HARNESS_CONFIG"])
        elif Path("config.json").exists():
            cfg_path = Path("config.json").resolve()
        else:
            cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    cfg = load(cfg_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("pending", "active", "done", "failed", "parked", "review"):
        (cfg.queue_dir / sub).mkdir(parents=True, exist_ok=True)
    store = StatsStore(cfg.stats_path)
    runner = SessionRunner(cfg, store, log=_log)
    provider = create_provider(cfg)
    pipeline = Pipeline(cfg, runner, log=_log, provider=provider)
    return cfg, store, runner, provider, pipeline