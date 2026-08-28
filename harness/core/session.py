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

from . import prompts
from .config import Config
from .enums import Stage, Verdict
from .stats import SessionRecord, StatsStore
from external.pi_cli import run_pi_session, _extract_verdict, _now


@dataclass
class SessionResult:
    ok: bool
    verdict: Verdict
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
        stage: Stage | str = "unknown",
        slice_id: str | None = None,
        iteration: int = 1,
        notes: str = "",
    ) -> SessionResult:
        workdir = Path(workdir)
        # Wire-side conversion, the one place a `Stage` becomes its string. A
        # stray string is recorded verbatim rather than raising: losing a stats
        # row is worse than a row with an unrecognised stage label.
        stage_value = stage.value if isinstance(stage, Stage) else stage
        out_file = workdir / f".pi-session-{stage_value}-{int(time.time())}.out"

        # Per-model budget: the smaller of the global tokenBudget and the model's
        # real context window (minus output headroom). This is the ceiling the
        # model is told to stay under, so it never overflows the window.
        budget = self.cfg.model_budget(model)
        self.log(f"  ▶ {stage_value} model={model} iter={iteration} "
                 f"budget={budget}k ctx={self.cfg.model_context(model)}k")

        # Prepend the per-model context-budget note so the model knows its own
        # ceiling (smaller for 32k/64k models, ~100k for 128k models).
        full_prompt = prompts.CONTEXT_BUDGET_NOTE.format(budget_k=budget // 1000) + prompt

        # Run the pi subprocess using the external module
        result = run_pi_session(
            model=model,
            workdir=workdir,
            prompt=full_prompt,
            out_file=out_file,
            log=self.log,
        )
        
        # `result.output` is assistant text only (T17 removed the stderr splice),
        # so stderr can never fabricate a verdict here.
        # Process edge: the model's `VERDICT:` line is a raw string. Convert it
        # to the enum exactly once here; everything downstream compares members.
        raw = _extract_verdict(result.output)
        verdict = Verdict.parse(raw) or Verdict.UNKNOWN
        if result.crashed and verdict is Verdict.UNKNOWN:
            verdict = Verdict.ERROR
        self.store.record(SessionRecord(
            ts=_now(),
            task_id=task_id,
            stage=stage_value,
            model=model,
            verdict=verdict.value,
            outcome=_outcome(verdict.value),
            peak_tokens=result.peak_tokens,
            duration_s=round(result.duration_s, 1),
            rc=result.rc,
            prompt_chars=len(prompt),
            slice=slice_id,
            iteration=iteration,
            notes=notes + (f" [crashed: {result.err[:120]}]" if result.crashed else ""),
        ))
        self.log(f"  ◀ {stage_value} rc={result.rc} tokens={result.peak_tokens} verdict={verdict} "
                 f"crashed={result.crashed} ({result.duration_s:.0f}s)")

        # Diagnostic: when the session came back empty or unparseable, log the
        # raw output tail so we can see what pi actually returned.
        if verdict is Verdict.UNKNOWN or result.peak_tokens == 0:
            tail = result.output.strip()[-300:]
            self.log(f"  … {stage_value} DIAG: verdict={verdict} tokens={result.peak_tokens} "
                     f"output_len={len(result.output)} tail={tail!r}")

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
    """Map a verdict *wire value* to the stats `outcome` column. Takes and returns
    a plain string — it feeds the JSONL row, so it is a wire-side function."""
    return verdict if verdict in (
        "pass", "fail", "kickback", "kickout", "done",
        "progress", "resliced", "error",
    ) else "unknown"
