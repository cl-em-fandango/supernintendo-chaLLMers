"""Run one fresh, token-budgeted pi session and record stats.

Uses `pi --mode json --no-session` so we can stream usage events and compute
peak context tokens without persisting a session file.

Resilience:
- A heartbeat thread logs every N seconds so a hung session is visible, not silent.
- If the pi process dies (crash/OOM) we detect it, record the failure, and return
  a non-ok result so the calling stage can retry. Artifacts the model already
  wrote to disk are preserved, so a retry continues rather than starting over.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .stats import SessionRecord, StatsStore
from external.pi_cli import run_pi_session, _extract_verdict, _now


@dataclass
class SessionResult:
    ok: bool
    verdict: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path
    crashed: bool = False


class SessionRunner:
    def __init__(self, cfg: Config, store: StatsStore, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log

    def run(
        self,
        model: str,
        workdir: str | Path,
        prompt: str,
        *,
        task_id: str | None = None,
        stage: str = "unknown",
        slice_id: str | None = None,
        iteration: int = 1,
        notes: str = "",
    ) -> SessionResult:
        workdir = Path(workdir)
        out_file = workdir / f".pi-session-{stage}-{int(time.time())}.out"

        self.log(f"  ▶ {stage} model={model} iter={iteration} budget={self.cfg.token_budget}k")
        
        # Run the pi subprocess using the external module
        result = run_pi_session(
            model=model,
            workdir=workdir,
            prompt=prompt,
            out_file=out_file,
            log=self.log,
        )
        
        verdict = _extract_verdict(result.output)
        if result.crashed and verdict == "unknown":
            verdict = "error"
        self.store.record(SessionRecord(
            ts=_now(),
            task_id=task_id,
            stage=stage,
            model=model,
            verdict=verdict,
            outcome=_outcome(verdict),
            peak_tokens=result.peak_tokens,
            duration_s=round(result.duration_s, 1),
            rc=result.rc,
            prompt_chars=len(prompt),
            slice=slice_id,
            iteration=iteration,
            notes=notes + (f" [crashed: {result.err[:120]}]" if result.crashed else ""),
        ))
        self.log(f"  ◀ {stage} rc={result.rc} tokens={result.peak_tokens} verdict={verdict} "
                 f"crashed={result.crashed} ({result.duration_s:.0f}s)")

        return SessionResult(
            ok=result.rc == 0 and not result.crashed,
            verdict=verdict,
            peak_tokens=result.peak_tokens,
            duration_s=result.duration_s,
            output=result.output,
            out_file=result.out_file,
            crashed=result.crashed,
        )





def _outcome(verdict: str) -> str:
    return verdict if verdict in (
        "pass", "fail", "kickback", "kickout", "done",
        "progress", "resliced", "error",
    ) else "unknown"
