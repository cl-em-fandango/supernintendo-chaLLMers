"""Configuration loading."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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

    def model_context(self, model: str) -> int:
        """Real context window (tokens) for a model.

        Every model is 128k unless its name states a smaller window
        (e.g. QwenOptimised32k = 32k). Unknown models default to 128k.
        """
        if model in self.model_context_map:
            return self.model_context_map[model]
        for suffix in ("32k", "64k", "128k"):
            if model.lower().endswith(suffix):
                return int(suffix[:-1]) * 1024
        return 131072

    def model_budget(self, model: str) -> int:
        """Working-context budget for a model: the smaller of the global
        tokenBudget and the model's real context window, minus a reserve for
        the model's own output. This is the ceiling the prompt tells the model
        to stay under, so it never overflows the window."""
        reserve = 8192  # headroom for the model's output tokens
        return max(4096, min(self.token_budget, self.model_context(model) - reserve))

    def get(self, key: str, default=None):
        return self.raw.get(key, default)


def load(path: str | Path) -> Config:
    p = Path(path)
    raw: dict[str, Any] = json.loads(p.read_text())
    work_dir = Path(raw["workDir"]).expanduser()
    return Config(
        work_dir=work_dir,
        token_budget=int(raw.get("tokenBudget", 100_000)),
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
