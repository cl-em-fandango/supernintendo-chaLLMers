"""Configuration loading."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Window assumed for a model whose window is neither mapped nor spelled out in
# its name. Everything on the local server is 128k unless stated otherwise.
DEFAULT_CONTEXT_WINDOW = 131072

# Working prompt cap fixed by decision D2: the moment usage crosses this the
# session is parked and handed off (the trip itself is T42).
DEFAULT_MAX_PROMPT_TOKENS = 60_000

# Headroom reserved for the model's own output tokens.
CONTEXT_RESERVE = 8192

# Never budget below this, however small the window.
MIN_MODEL_BUDGET = 4096


@dataclass
class Config:
    work_dir: Path
    token_budget: int
    max_spec_kickbacks: int
    max_slice_implement: int
    max_slice_tech_review: int
    max_slice_func_review: int
    max_slice_check_loops: int
    autonomous_queue_target: int
    trunk_branch: str
    task_provider: str
    directory_provider: dict
    models: dict
    model_context_map: dict
    repo_dir: Path | None = None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def queue_dir(self) -> Path:
        return self.work_dir / "queue"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def sessions_dir(self) -> Path:
        return self.work_dir / "sessions"

    @property
    def stats_path(self) -> Path:
        return self.work_dir / "stats" / "sessions.jsonl"

    @property
    def model(self) -> str:
        return self.models["technicalWriter"]

    @property
    def implementer(self) -> str:
        return self.models["implementer"]

    @property
    def assessor(self) -> str:
        return self.models["assessor"]

    @property
    def random_pool(self) -> list[str]:
        return list(self.models.get("randomPool", []))

    @property
    def fast_pool(self) -> list[str]:
        """Fast (MOE / A3B-class) models for high-volume, low-stakes stages."""
        pool = self.models.get("fastPool")
        if pool:
            return list(pool)
        # fallback: auto-detect MOE/A3B-class names from the random pool
        return [m for m in self.random_pool
                if any(k in m for k in ("A3B", "MOE", "MoE", "moe", "Gemma", "oss"))]

    @property
    def max_prompt_tokens(self) -> int:
        """Working prompt cap in tokens (config key `maxPromptTokens`).

        A *cap*, not a window: the window is `model_context()`. `token_budget`
        is the legacy spelling of the same number and is kept as the fallback
        so hand-built configs from before the key keep working.
        """
        return int(self.raw.get("maxPromptTokens", self.token_budget))

    def _known_context(self, model: str) -> int | None:
        """The window for `model` if we actually know it, else None."""
        if model in self.model_context_map:
            return self.model_context_map[model]
        for suffix in ("32k", "64k", "128k"):
            if model.lower().endswith(suffix):
                return int(suffix[:-1]) * 1024
        return None

    def model_context(self, model: str) -> int:
        """Real context window (tokens) for a model.

        Every model is 128k unless its name states a smaller window
        (e.g. QwenOptimised32k = 32k). Unknown models default to 128k — a
        caller that wants to warn about that default checks
        `has_known_context()` first; this lookup stays silent.
        """
        known = self._known_context(model)
        return DEFAULT_CONTEXT_WINDOW if known is None else known

    def has_known_context(self, model: str) -> bool:
        """True when the window comes from the config map or the name suffix,
        i.e. `model_context()` is not falling back to the 128k default."""
        return self._known_context(model) is not None

    def model_budget(self, model: str) -> int:
        """Working-context budget for a model: the prompt cap, but never so
        large that the window minus output headroom is exceeded.

        `max(4096, min(max_prompt_tokens, model_context(model) - 8192))`. With
        the shipped config (cap 60000) a 128k model budgets 60000 — the cap
        binds — and a 64k model budgets 57344, because 65536 - 8192 = 57344 is
        below the cap. This is the ceiling the prompt tells the model to stay
        under, so it never overflows the window.
        """
        return max(MIN_MODEL_BUDGET,
                   min(self.max_prompt_tokens,
                       self.model_context(model) - CONTEXT_RESERVE))

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def load(path: str | Path) -> Config:
    p = Path(path)
    raw: dict[str, Any] = json.loads(p.read_text())
    work_dir = Path(raw["workDir"]).expanduser()
    repo_dir_raw = (
        raw.get("repoDir")
        or raw.get("repoPath")
        or raw.get("repo_dir")
        or raw.get("repo")
        or raw.get("targetRepo")
    )
    repo_dir = Path(repo_dir_raw).expanduser().resolve() if repo_dir_raw else None
    return Config(
        work_dir=work_dir,
        repo_dir=repo_dir,
        token_budget=int(raw.get("maxPromptTokens",
                                raw.get("tokenBudget",
                                        DEFAULT_MAX_PROMPT_TOKENS))),
        max_spec_kickbacks=int(raw.get("maxSpecKickbacks", 3)),
        max_slice_implement=int(raw.get("maxSliceImplement", 5)),
        max_slice_tech_review=int(raw.get("maxSliceTechReview", 5)),
        max_slice_func_review=int(raw.get("maxSliceFuncReview", 5)),
        max_slice_check_loops=int(raw.get("maxSliceCheckLoops", 3)),
        autonomous_queue_target=int(raw.get("autonomousQueueTarget", 5)),
        trunk_branch=raw.get("trunkBranch", "pi/trunk"),
        task_provider=raw.get("taskProvider", "directory"),
        directory_provider=raw.get("directoryProvider", {}),
        models=raw.get("models", {}),
        model_context_map={k: int(v) for k, v in raw.get("modelContext", {}).items()},
        raw=raw,
    )
