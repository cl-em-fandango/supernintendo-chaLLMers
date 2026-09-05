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
from .config import Config, DEFAULT_CONTEXT_WINDOW
from .enums import Stage, Verdict
from .stats import SessionRecord, StatsStore
from .transcripts import (
    TranscriptRecord,
    next_sequence,
    resolve_task_dir,
    write_pooled_transcript,
    write_transcript,
)
from external.pi_cli import (
    PiSessionResult,
    run_pi_session,
    validate_models_present,
    _extract_verdict,
    _now,
)


@dataclass
class SessionResult:
    """A finished session.

    `over_context_budget` is the streamed over-cap stop (T48) lifted into this
    layer: usage went strictly over `context_limit` and the child was stopped for
    that reason. It is deliberately not folded into `crashed` — a session we
    stopped on purpose and a child that died stay tellable apart, and the child's
    own return code stays in the row's `rc` either way.
    """
    ok: bool
    verdict: Verdict
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path
    crashed: bool = False
    over_context_budget: bool = False
    context_limit: int | None = None


class SessionRunner:
    def __init__(self, cfg: Config, store: StatsStore, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log
        self._models_validated = False

    def validate_models(self) -> None:
        """Validate all configured models via pi --list-models before running."""
        if self._models_validated:
            return
        models = getattr(self.cfg, "configured_models", None)
        if callable(models):
            models = models()
        if models and isinstance(models, (list, tuple, set)):
            validate_models_present(models, log=self.log)
        self._models_validated = True

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
        self.validate_models()
        workdir = Path(workdir)
        # Wire-side conversion, the one place a `Stage` becomes its string. A
        # stray string is recorded verbatim rather than raising: losing a stats
        # row is worse than a row with an unrecognised stage label.
        stage_value = stage.value if isinstance(stage, Stage) else stage
        out_file = workdir / f".pi-session-{stage_value}-{int(time.time())}.out"

        # A window we had to guess is worth saying out loud: it is the one case
        # where the budget below is derived from a default, not from config.
        if not self.cfg.has_known_context(model):
            self.log(f"  unknown context window for {model}, "
                     f"assuming {DEFAULT_CONTEXT_WINDOW}")

        # Per-model budget: the working prompt cap, clamped to the model's real
        # window minus output headroom. This is the ceiling the model is told to
        # stay under, so it never overflows the window.
        budget = self.cfg.model_budget(model)
        # Both numbers are raw token counts, so both are labelled `tokens` — a
        # `k` suffix on an unscaled integer would read as a thousand-fold lie.
        self.log(f"  ▶ {stage_value} model={model} iter={iteration} "
                 f"budget={budget} tokens ctx={self.cfg.model_context(model)} tokens")

        # Prepend the per-model context-budget note so the model knows its own
        # ceiling (smaller for 32k/64k models, ~100k for 128k models).
        full_prompt = prompts.CONTEXT_BUDGET_NOTE.format(budget_k=budget // 1000) + prompt

        # Run the pi subprocess using the external module.
        # The ceiling handed to the stream is the configured cap
        # (`maxPromptTokens`), the one threshold decision D2 names — not the
        # per-model `budget` above. The budget is what the prompt tells the model
        # to aim under; this is the hard stop for a session that ignores it.
        # Handing it down is what makes the check run on every streamed usage
        # value instead of after the session is already over.
        import inspect
        sig = inspect.signature(run_pi_session)
        kwargs = {
            "model": model,
            "workdir": workdir,
            "prompt": full_prompt,
            "out_file": out_file,
            "log": self.log,
            "max_context_tokens": self.cfg.max_prompt_tokens,
        }
        # The hard wall-clock cap for the session: the config key
        # `sessionTimeout` (default 3600s), handed down so a wedged pi is
        # group-killed on the configured clock instead of pi_cli's fallback.
        if "timeout_s" in sig.parameters:
            kwargs["timeout_s"] = self.cfg.session_timeout
        if "ui_context" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            kwargs["ui_context"] = {
                "task_id": task_id,
                "stage": stage_value,
                "slice_id": slice_id,
                "iteration": iteration,
                "budget": budget,
                "model": model,
            }
        result = run_pi_session(**kwargs)

        # Transcript sequence is read *before* this session's stats row exists
        # so the first session of a task is `001`; the on-disk count inside
        # `next_sequence` keeps a resumed task's numbering past its restored
        # transcripts. A *provided* task id with no queue directory is the
        # C2 case: the skip gets an explicit warning naming the stage and the
        # reason, so "no transcript" is never silent. A genuinely absent task
        # id (`task_id is None`, the autonomous loop) is *not* the C2 case and
        # stays quiet here — its recording is FR-7's pooled pool, not a skip.
        transcript_task_dir = (
            resolve_task_dir(self.cfg.queue_dir, task_id) if task_id else None
        )
        if task_id and transcript_task_dir is None:
            self.log(f"  ! no transcript written for stage {stage_value}: "
                     f"task dir not found for {task_id}")
        transcript_seq = (
            next_sequence(len(self.store.for_task(task_id)), transcript_task_dir)
            if transcript_task_dir is not None else None
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
            notes=_row_notes(notes, result),
        ))
        self.log(f"  ◀ {stage_value} rc={result.rc} tokens={result.peak_tokens} verdict={verdict} "
                 f"crashed={result.crashed} ({result.duration_s:.0f}s)")

        transcript = TranscriptRecord(
            sequence=transcript_seq,
            task_id=task_id,
            stage=stage_value,
            timestamp=_now(),
            model=model,
            duration_s=round(result.duration_s, 1),
            peak_tokens=result.peak_tokens,
            rc=result.rc,
            verdict=verdict.value,
            crashed=result.crashed,
            prompt=full_prompt,
            output=result.output,
            # The Stderr section is the child's stderr and nothing else;
            # the run's own failure text (`err`) is metadata, so a session
            # that died with no stderr still shows an empty Stderr fence.
            stderr=result.stderr,
            error=result.err or "",
            slice_id=slice_id,
            iteration=iteration,
        )
        # FR-6 cleanup ordering is the whole point: the workdir
        # `.out`/`.err` capture is deleted *only after* its content is durably
        # in a transcript (either pool). A failed write keeps the capture —
        # deleting it first would throw away the only copy of the stderr.
        # A session that gets no transcript at all (unresolvable task id, the
        # C2 skip) keeps the legacy capture placement: content is never lost
        # twice.
        if transcript_task_dir is not None:
            transcript_path = write_transcript(transcript_task_dir,
                                               transcript, self.log)
            if transcript_path is not None:
                _cleanup_transient_capture(result.out_file, self.log)
        elif task_id is None:
            # FR-7: the autonomous loop has no task dir and no journey to
            # link from, so its sessions land in the work-dir transcript pool
            # instead of being skipped. The log line names the pool file; a
            # failed pooled write emits its own skip-warning (FR-5 C4
            # fallback), keeps the capture, and the run continues.
            pool_path = write_pooled_transcript(self.cfg.harness_execution_and_queue_dir,
                                                transcript, self.log)
            if pool_path is not None:
                self.log(f"  · pooled transcript: {pool_path}")
                _cleanup_transient_capture(result.out_file, self.log)

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
            over_context_budget=result.over_context_budget,
            context_limit=result.context_limit,
        )





def _cleanup_transient_capture(out_file: Path | None, log) -> None:
    """Delete the session's workdir `.out`/`.err` capture files.

    Best-effort by design: the transcript is already on disk, so a capture we
    cannot delete (permissions, an already-removed file) is worth a warning
    line, never a failed session. The `.err` sibling is the path
    `external/pi_cli` writes (`<out_file>.err`) and only exists when the
    child produced stderr.
    """
    if out_file is None:
        return
    for path in (Path(out_file), Path(str(out_file) + ".err")):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log(f"  ! could not remove transient session capture {path}: {exc}")


def _row_notes(notes: str, result: PiSessionResult) -> str:
    """Fold the run's anomalies into the one `notes` string of one stats row.

    Built before the row is appended, so a session stays exactly one row: an
    over-cap stop and a dead child are both annotations on the same record,
    never a second append and never a rewrite of the JSONL. The over-cap
    annotation carries both numbers because the row is what an operator reads
    afterwards — the measured peak and the ceiling that stopped the session.
    """
    if result.over_context_budget:
        notes += (f" over-cap peak={result.peak_tokens}"
                  f" limit={result.context_limit}")
    if result.crashed:
        notes += f" [crashed: {result.err[:120]}]"
    return notes


def _outcome(verdict: str) -> str:
    """Map a verdict *wire value* to the stats `outcome` column. Takes and returns
    a plain string — it feeds the JSONL row, so it is a wire-side function."""
    return verdict if verdict in (
        "pass", "fail", "kickback", "kickout", "done",
        "progress", "resliced", "error",
    ) else "unknown"
