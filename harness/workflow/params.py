"""Named state objects for the pipeline (CODING_STANDARDS §2/§5)."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StageContext:
    """Everything a stage needs, named instead of positional path tuples."""
    task_id: str
    task_dir: Path      # the active/<task_id> dir
    workdir: Path       # where the git repo / code lives