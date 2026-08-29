"""Unified historical store for per-session stats.

Every pi invocation is recorded as one JSONL row in <workDir>/stats/sessions.jsonl.
Rows are append-only; analytics are computed on read.

Row schema:
{
  "ts":            ISO timestamp (start),
  "task_id":       str | None,
  "stage":         spec_author | spec_assess_ornith | spec_assess_tw |
                   feasibility | slicing | slice_check |
                   slice_implement | slice_fix | tech_review | func_review |
                   holistic | autonomous_suggest | autonomous_review,
  "slice":         str | None,
  "iteration":     int,
  "model":         str,
  "prompt_chars":  int,
  "duration_s":    float,
  "peak_tokens":   int,
  "verdict":       str,
  "outcome":       pass | fail | kickback | kickout | done | progress |
                   resliced | error | unknown,
  "rc":            int,
  "session_file":  str | None,
  "notes":         str
}
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator


@dataclass
class SessionRecord:
    ts: str
    task_id: str | None
    stage: str
    model: str
    verdict: str
    outcome: str
    peak_tokens: int
    duration_s: float
    rc: int
    prompt_chars: int = 0
    slice: str | None = None
    iteration: int = 1
    session_file: str | None = None
    notes: str = ""


class StatsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def record(self, rec: SessionRecord) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(rec)) + "\n")

    def all(self) -> list[dict]:
        out = []
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return out

    def for_task(self, task_id: str) -> list[dict]:
        return [r for r in self.all() if r.get("task_id") == task_id]

    def write_task_journey(self, task_id: str, dest_dir: Path | None = None) -> Path:
        """Write a static workflow journey graph file for the task."""
        out_dir = Path(dest_dir) if dest_dir else self.path.parent / "journeys"
        out_dir.mkdir(parents=True, exist_ok=True)
        journey_path = out_dir / f"{task_id}-journey.txt"
        rows = self.for_task(task_id)
        journey_path.write_text(render_task_journey(rows, task_id=task_id))
        return journey_path


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def _group(rows: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(str(r.get(key, "unknown")), []).append(r)
    return out


def model_report(rows: list[dict]) -> list[dict]:
    """Per-model quality/speed stats.

    quality signals:
      rejection_rate  = (kickback + fail + kickout) / decided sessions
                        (sessions where this model's verdict rejected work)
      pass_rate       = pass/done/resliced / decided
      error_rate      = error / all
    speed signals:
      avg_duration_s, avg_peak_tokens
    """
    report = []
    for model, rs in sorted(_group(rows, "model").items()):
        n = len(rs)
        decided = [r for r in rs if r["outcome"] not in ("error", "unknown")]
        rejections = [r for r in decided if r["outcome"] in ("kickback", "fail", "kickout")]
        passes = [r for r in decided if r["outcome"] in ("pass", "done", "resliced")]
        errors = [r for r in rs if r["outcome"] == "error"]
        report.append({
            "model": model,
            "sessions": n,
            "rejections": len(rejections),
            "rejection_rate": round(len(rejections) / len(decided), 3) if decided else None,
            "pass_rate": round(len(passes) / len(decided), 3) if decided else None,
            "error_rate": round(len(errors) / n, 3) if n else None,
            "avg_duration_s": round(sum(r["duration_s"] for r in rs) / n, 1) if n else None,
            "avg_peak_tokens": int(sum(r["peak_tokens"] for r in rs) / n) if n else None,
            "total_tokens": sum(r["peak_tokens"] for r in rs),
        })
    return report


def stage_report(rows: list[dict]) -> list[dict]:
    """Per-stage stats: how often work gets bounced at each stage."""
    report = []
    for stage, rs in sorted(_group(rows, "stage").items()):
        n = len(rs)
        bounces = [r for r in rs if r["outcome"] in ("kickback", "fail", "kickout")]
        report.append({
            "stage": stage,
            "sessions": n,
            "bounces": len(bounces),
            "bounce_rate": round(len(bounces) / n, 3) if n else None,
            "avg_duration_s": round(sum(r["duration_s"] for r in rs) / n, 1) if n else None,
            "avg_peak_tokens": int(sum(r["peak_tokens"] for r in rs) / n) if n else None,
        })
    return report


def task_report(rows: list[dict]) -> list[dict]:
    report = []
    for task, rs in sorted(_group(rows, "task_id").items()):
        report.append({
            "task_id": task,
            "sessions": len(rs),
            "total_tokens": sum(r["peak_tokens"] for r in rs),
            "total_duration_s": round(sum(r["duration_s"] for r in rs), 1),
            "bounces": sum(1 for r in rs if r["outcome"] in ("kickback", "fail", "kickout")),
        })
    return report


def render_report(rows: list[dict]) -> str:
    lines = []
    lines.append(f"=== Session stats ({len(rows)} total) ===\n")

    lines.append("--- By model (quality & speed) ---")
    lines.append(f"{'model':<52} {'sess':>5} {'rej%':>6} {'pass%':>6} {'err%':>6} {'avg_s':>7} {'avg_tok':>8}")
    for m in model_report(rows):
        lines.append(
            f"{m['model'][:52]:<52} {m['sessions']:>5} "
            f"{_pct(m['rejection_rate']):>6} {_pct(m['pass_rate']):>6} {_pct(m['error_rate']):>6} "
            f"{m['avg_duration_s'] or 0:>7.1f} {m['avg_peak_tokens'] or 0:>8}"
        )

    lines.append("\n--- By stage (bounce rate) ---")
    lines.append(f"{'stage':<22} {'sess':>5} {'bounce%':>8} {'avg_s':>7} {'avg_tok':>8}")
    for s in stage_report(rows):
        lines.append(
            f"{s['stage']:<22} {s['sessions']:>5} {_pct(s['bounce_rate']):>8} "
            f"{s['avg_duration_s'] or 0:>7.1f} {s['avg_peak_tokens'] or 0:>8}"
        )

    lines.append("\n--- By task ---")
    for t in task_report(rows):
        lines.append(
            f"{t['task_id']:<40} sessions={t['sessions']:<4} "
            f"tokens={t['total_tokens']:<9} time={t['total_duration_s']:>8.1f}s bounces={t['bounces']}"
        )
    return "\n".join(lines)


def _pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else "  -"


# ---------------------------------------------------------------------------
# Workflow Journey Analysis & Static Graph Readout
# ---------------------------------------------------------------------------

@dataclass
class JourneyStep:
    """A single step in a task's workflow journey."""
    index: int
    stage: str
    slice_id: str | None
    iteration: int
    model: str
    duration_s: float
    peak_tokens: int
    verdict: str
    outcome: str
    notes: str
    is_bounce: bool
    is_loop: bool
    is_hotspot: bool
    time_pct: float
    flow_symbol: str


@dataclass
class JourneyAnalysis:
    """Holistic workflow analysis detailing loops, blockages, and latency hotspots."""
    task_id: str
    steps: list[JourneyStep]
    total_sessions: int
    total_duration_s: float
    max_peak_tokens: int
    total_tokens: int
    bounces_count: int
    loops_count: int
    hotspots: list[JourneyStep]
    loop_descriptions: list[str]
    bounce_descriptions: list[str]


def task_journey_analysis(rows: list[dict], task_id: str | None = None) -> JourneyAnalysis:
    """Analyze the journey of a task through the pipeline stages."""
    if not task_id and rows:
        task_id = str(rows[0].get("task_id") or "unknown")
    task_id = task_id or "unknown"

    total_sessions = len(rows)
    total_duration_s = sum(float(r.get("duration_s", 0.0)) for r in rows)
    max_peak_tokens = max((int(r.get("peak_tokens", 0)) for r in rows), default=0)
    total_tokens = sum(int(r.get("peak_tokens", 0)) for r in rows)

    steps: list[JourneyStep] = []
    bounces_count = 0
    loop_descriptions: list[str] = []
    bounce_descriptions: list[str] = []

    for idx, r in enumerate(rows, 1):
        stage = str(r.get("stage", "unknown"))
        slice_id = str(r.get("slice")) if r.get("slice") is not None else None
        iteration = int(r.get("iteration", 1))
        model = str(r.get("model", "unknown"))
        duration_s = float(r.get("duration_s", 0.0))
        peak_tokens = int(r.get("peak_tokens", 0))
        verdict = str(r.get("verdict", "unknown"))
        outcome = str(r.get("outcome", "unknown"))
        notes = str(r.get("notes", ""))

        is_bounce = outcome in ("kickback", "fail", "kickout", "error") or verdict.lower() in ("kickback", "fail", "error", "rejected")
        is_loop = iteration > 1 or "retry" in notes.lower() or "fix" in stage.lower() or "fix" in notes.lower()
        time_pct = (duration_s / total_duration_s * 100.0) if total_duration_s > 0 else 0.0
        is_hotspot = duration_s >= 60.0 or time_pct >= 25.0

        if is_bounce:
            bounces_count += 1
            bounce_descriptions.append(
                f"Step #{idx} [{stage}{' (slice ' + slice_id + ')' if slice_id else ''}]: "
                f"{verdict.upper()} / {outcome.upper()} (model: {model}, {duration_s:.1f}s)"
            )

        if idx == len(rows):
            if is_bounce:
                flow_symbol = "───► [BLOCKED ⛔]"
            elif verdict.lower() in ("done", "pass"):
                flow_symbol = "───► [COMPLETE ✔]"
            else:
                flow_symbol = "───► [FINISH]"
        elif is_bounce:
            flow_symbol = "───┐ [BOUNCE ↩]"
        elif is_loop:
            flow_symbol = f"◄──┘ [LOOP #{iteration}] ───►"
        else:
            flow_symbol = "───►"

        steps.append(JourneyStep(
            index=idx,
            stage=stage,
            slice_id=slice_id,
            iteration=iteration,
            model=model,
            duration_s=duration_s,
            peak_tokens=peak_tokens,
            verdict=verdict,
            outcome=outcome,
            notes=notes,
            is_bounce=is_bounce,
            is_loop=is_loop,
            is_hotspot=is_hotspot,
            time_pct=time_pct,
            flow_symbol=flow_symbol,
        ))

    loop_steps = [s for s in steps if s.is_loop]
    loops_count = len(loop_steps)
    for s in loop_steps:
        target_name = f"{s.stage}{' (slice ' + s.slice_id + ')' if s.slice_id else ''}"
        loop_descriptions.append(
            f"Step #{s.index} [{target_name}]: iteration {s.iteration} ({s.notes or 'retry/fix'})"
        )

    hotspots = sorted([s for s in steps if s.is_hotspot or s.duration_s >= 30.0],
                      key=lambda x: x.duration_s, reverse=True)
    if not hotspots and steps:
        hotspots = sorted(steps, key=lambda x: x.duration_s, reverse=True)[:2]

    return JourneyAnalysis(
        task_id=task_id,
        steps=steps,
        total_sessions=total_sessions,
        total_duration_s=round(total_duration_s, 1),
        max_peak_tokens=max_peak_tokens,
        total_tokens=total_tokens,
        bounces_count=bounces_count,
        loops_count=loops_count,
        hotspots=hotspots,
        loop_descriptions=loop_descriptions,
        bounce_descriptions=bounce_descriptions,
    )


def render_task_journey(rows: list[dict], task_id: str | None = None) -> str:
    """Render a comprehensive, visual ASCII workflow journey graph for a task."""
    if not rows:
        return f"No sessions recorded for task '{task_id or 'unknown'}'."

    analysis = task_journey_analysis(rows, task_id=task_id)
    lines: list[str] = []

    lines.append("=" * 100)
    lines.append(f"WORKFLOW JOURNEY GRAPH: {analysis.task_id}")
    lines.append("=" * 100)
    lines.append(
        f"Total Sessions: {analysis.total_sessions} | "
        f"Wall Clock Time: {analysis.total_duration_s:.1f}s | "
        f"Max Tokens: {_format_tokens(analysis.max_peak_tokens)} | "
        f"Bounces/Blocks: {analysis.bounces_count} | "
        f"Loops/Retries: {analysis.loops_count}"
    )
    lines.append("-" * 100)

    lines.append("\n─── CHRONOLOGICAL JOURNEY FLOW ──────────────────────────────────────────────────────────")
    header_fmt = f"{'#':<3} {'Stage / Target':<28} {'Model':<30} {'Duration':>9} {'Tokens':>9} {'Verdict':<10} {'Flow Graph'}"
    lines.append(header_fmt)
    lines.append("─" * 100)

    for s in analysis.steps:
        target_name = s.stage
        if s.slice_id:
            target_name = f"slice {s.slice_id}: {s.stage.replace('slice_', '')}"
        if s.iteration > 1:
            target_name += f" (iter {s.iteration})"

        dur_str = f"{s.duration_s:.1f}s"
        if s.is_hotspot:
            dur_str += " 🔥"

        tok_str = _format_tokens(s.peak_tokens)
        model_str = s.model[:30]
        verdict_str = f"{s.verdict[:10]}"

        row_line = (
            f"{s.index:<3} "
            f"{target_name[:28]:<28} "
            f"{model_str:<30} "
            f"{dur_str:>9} "
            f"{tok_str:>9} "
            f"{verdict_str:<10} "
            f"{s.flow_symbol}"
        )
        lines.append(row_line)

    lines.append("─" * 100)

    lines.append("\n─── JOURNEY DIAGNOSTICS & BOTTLENECK ANALYSIS ──────────────────────────────────────────")

    lines.append(f"\n🔄 LOOPS & RETRIES ({analysis.loops_count} detected):")
    if analysis.loop_descriptions:
        for desc in analysis.loop_descriptions:
            lines.append(f"   • {desc}")
    else:
        lines.append("   • Clean straight pass (no retry loops)")

    lines.append(f"\n⛔ BLOCKAGES & BOUNCES ({analysis.bounces_count} detected):")
    if analysis.bounce_descriptions:
        for desc in analysis.bounce_descriptions:
            lines.append(f"   • {desc}")
    else:
        lines.append("   • No rejections or blockages encountered")

    lines.append(f"\n⏱️ TIME HOTSPOTS (Where it took ages):")
    if analysis.hotspots:
        for idx, h in enumerate(analysis.hotspots[:5], 1):
            tag = " [🔥 HOTSPOT]" if h.is_hotspot else ""
            h_name = f"{h.stage}{' (slice ' + h.slice_id + ')' if h.slice_id else ''}"
            lines.append(
                f"   • {idx}. Step #{h.index} {h_name}: {h.duration_s:.1f}s "
                f"({h.time_pct:.1f}% of task time, {h.model}){tag}"
            )
    else:
        lines.append("   • Execution time was evenly distributed")

    lines.append(f"\n📊 STAGE SUMMARY FOR TASK {analysis.task_id}:")
    st_group = _group(rows, "stage")
    for st_name, st_rows in sorted(st_group.items()):
        st_sess = len(st_rows)
        st_dur = sum(float(r.get("duration_s", 0.0)) for r in st_rows)
        st_max_tok = max((int(r.get("peak_tokens", 0)) for r in st_rows), default=0)
        st_bounces = sum(1 for r in st_rows if r.get("outcome") in ("kickback", "fail", "kickout", "error"))
        lines.append(
            f"   • {st_name:<20}: {st_sess:>2} session{'s' if st_sess != 1 else ' '} | "
            f"time={st_dur:>7.1f}s | "
            f"max_tok={_format_tokens(st_max_tok):>7} | "
            f"bounces={st_bounces}"
        )

    lines.append("\n" + "=" * 100)
    return "\n".join(lines)


def _format_tokens(n: int) -> str:
    """Format token count with k suffix if >= 1000."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
