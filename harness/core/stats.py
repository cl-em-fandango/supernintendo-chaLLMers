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
