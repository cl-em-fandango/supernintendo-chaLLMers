"""Unified historical store for per-session stats.

# Existing code omitted for brevity"

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
    def prune(self, max_rows: int) -> None:
        """Trim the stats file to keep only the most recent ``max_rows`` entries.

        If the file has fewer rows than ``max_rows`` nothing is changed.
        The operation is atomic: a temporary file is written and then renamed.
        """
        rows = self.all()
        if len(rows) <= max_rows:
            return
        # Keep the newest ``max_rows`` rows (they are in chronological order)
        keep = rows[-max_rows:]
        tmp_path = self.path.with_suffix('.tmp')
        with tmp_path.open('w', encoding='utf-8') as f:
            for rec in keep:
                f.write(json.dumps(rec) + "\n")
        tmp_path.replace(self.path)

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


def render_task_journey_markdown(rows: list[dict], task_id: str | None = None,
                                 transcript_files: list[str | None] | None = None) -> str:
    """Render the task journey as a browsable Markdown document.

    Companion to `render_task_journey`, not a replacement: same analysis, same
    headline metrics and diagnostics, but every session row links to its
    transcript with a path *relative to `artifacts/`* so the directory stays
    browsable when it is moved or committed. `transcript_files` is positionally
    aligned with `rows` (one filename, or None when that session has no
    transcript — pre-feature sessions, or a transcript write that failed) and
    such a row shows an em dash instead of a link.
    """
    if not rows:
        return f"# Journey: {task_id or 'unknown'}\n\nNo sessions recorded.\n"

    analysis = task_journey_analysis(rows, task_id=task_id)
    links = list(transcript_files) if transcript_files else [None] * len(rows)
    lines: list[str] = [
        f"# Journey: {analysis.task_id}",
        "",
        (f"**Sessions:** {analysis.total_sessions} · "
         f"**Wall clock:** {analysis.total_duration_s:.1f}s · "
         f"**Max tokens:** {_format_tokens(analysis.max_peak_tokens)} · "
         f"**Bounces/blocks:** {analysis.bounces_count} · "
         f"**Loops/retries:** {analysis.loops_count}"),
        "",
        "## Flowchart",
        "",
        "```mermaid",
        *_mermaid_flowchart(analysis, links),
        "```",
        "",
        "## Sessions",
        "",
        "| # | Stage / Target | Model | Duration | Tokens | Verdict | Transcript |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for step, filename in zip(analysis.steps, links):
        lines.append(
            f"| {step.index} | {_escape_cell(_target_name(step))} "
            f"| {_escape_cell(step.model)} "
            f"| {step.duration_s:.1f}s "
            f"| {_format_tokens(step.peak_tokens)} "
            f"| {_escape_cell(step.verdict)} "
            f"| {_transcript_link(filename)} |"
        )
    lines += ["", "## Diagnostics", ""]
    lines += _markdown_list("Loops & retries", analysis.loop_descriptions,
                            "Clean straight pass (no retry loops)",
                            count=analysis.loops_count)
    lines += _markdown_list("Blockages & bounces", analysis.bounce_descriptions,
                            "No rejections or blockages encountered",
                            count=analysis.bounces_count)
    lines += _markdown_list("Time hotspots",
                            _hotspot_descriptions(analysis.hotspots),
                            "Execution time was evenly distributed")
    lines += _markdown_stage_summary(rows, analysis.task_id)
    return "\n".join(lines) + "\n"


MERMAID_NODE_ID_FMT = "N{}"
MERMAID_TRANSCRIPT_DIR = "sessions"

_MERMAID_ESCAPES = (
    ("#", "#35;"),      # entity marker first, or the rest would double-escape
    ('"', "#quot;"),
    ("[", "#91;"),
    ("]", "#93;"),
    ("|", "#124;"),
    ("`", "#96;"),
)


def _sanitize_mermaid(text: str) -> str:
    """Make arbitrary text safe inside a quoted Mermaid label or edge label.

    Quotes, brackets, pipes and backticks end a label or a diagram line; `#`
    starts a Mermaid entity. All become numeric/named entities so the diagram
    parses regardless of stage, model or verdict names. Non-ASCII (emoji, "→")
    passes through untouched — Mermaid reads it fine inside quotes.
    """
    cleaned = " ".join(str(text).split())
    for char, entity in _MERMAID_ESCAPES:
        cleaned = cleaned.replace(char, entity)
    return cleaned


def _mermaid_label(step: JourneyStep) -> str:
    """One node label: stage/slice/iteration and the verdict it ended on."""
    return _sanitize_mermaid(f"{_target_name(step)} → {step.verdict}")


def _mermaid_flowchart(analysis: JourneyAnalysis,
                       links: list[str | None]) -> list[str]:
    """The Mermaid `flowchart LR` body: nodes, edges, and one click per transcript.

    One node per session in chronological order; consecutive sessions are
    edged forward, with a dashed labelled edge where a bounce sent the work
    back (kickback/fail) or a loop re-entered a stage (retry n). Every node
    with a transcript carries a `click` to its file, relative to `artifacts/`;
    nodes without one (pre-feature sessions, failed writes) get no click line.
    """
    lines = ["flowchart LR"]
    for step in analysis.steps:
        node_id = MERMAID_NODE_ID_FMT.format(step.index)
        lines.append(f'    {node_id}["{_mermaid_label(step)}"]')
    for src, dst in zip(analysis.steps, analysis.steps[1:]):
        src_id = MERMAID_NODE_ID_FMT.format(src.index)
        dst_id = MERMAID_NODE_ID_FMT.format(dst.index)
        if src.is_bounce:
            label = _sanitize_mermaid(src.outcome)
            lines.append(f"    {src_id} -.->|{label}| {dst_id}")
        elif dst.is_loop:
            lines.append(f"    {src_id} -.->|retry {dst.iteration}| {dst_id}")
        else:
            lines.append(f"    {src_id} --> {dst_id}")
    for step, filename in zip(analysis.steps, links):
        if filename:
            lines.append(f'    click {MERMAID_NODE_ID_FMT.format(step.index)} '
                         f'"{MERMAID_TRANSCRIPT_DIR}/{filename}"')
    return lines


def _target_name(step: JourneyStep) -> str:
    """The `Stage / Target` label: slice and iteration read at a glance."""
    name = step.stage
    if step.slice_id:
        name = f"slice {step.slice_id}: {step.stage.replace('slice_', '')}"
    if step.iteration > 1:
        name += f" (iter {step.iteration})"
    return name


def _escape_cell(text: str) -> str:
    """Make text safe for a Markdown table cell (pipes break rows, newlines end them)."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _transcript_link(filename: str | None) -> str:
    """A relative link to one transcript, or an em dash when there is none."""
    if not filename:
        return "—"
    return f"[{filename}](sessions/{filename})"


def _markdown_list(title: str, items: list[str], empty_text: str,
                   count: int | None = None) -> list[str]:
    """One `###` diagnostics section as a Markdown list."""
    heading = f"### {title}"
    if count is not None:
        heading += f" ({count} detected)"
    lines = [heading, ""]
    lines += [f"- {_escape_cell(item)}" for item in items] or [f"- {empty_text}"]
    lines.append("")
    return lines


def _hotspot_descriptions(hotspots: list[JourneyStep]) -> list[str]:
    """The latency hotspots, slowest first, with their share of wall clock."""
    return [
        f"Step #{h.index} {_target_name(h)}: {h.duration_s:.1f}s "
        f"({h.time_pct:.1f}% of task time, {h.model})"
        + (" [HOTSPOT]" if h.is_hotspot else "")
        for h in hotspots[:5]
    ]


def _markdown_stage_summary(rows: list[dict], task_id: str) -> list[str]:
    """Per-stage totals for the task, ported from the ASCII readout."""
    lines = [f"### Stage summary for {task_id}", ""]
    for stage_name, stage_rows in sorted(_group(rows, "stage").items()):
        sessions = len(stage_rows)
        duration = sum(float(r.get("duration_s", 0.0)) for r in stage_rows)
        max_tokens = max((int(r.get("peak_tokens", 0)) for r in stage_rows),
                         default=0)
        bounces = sum(1 for r in stage_rows
                      if r.get("outcome") in ("kickback", "fail", "kickout", "error"))
        lines.append(
            f"- **{stage_name}**: {sessions} session{'s' if sessions != 1 else ''} "
            f"| time={duration:.1f}s "
            f"| max_tokens={_format_tokens(max_tokens)} "
            f"| bounces={bounces}"
        )
    lines.append("")
    return lines


def _format_tokens(n: int) -> str:
    """Format token count with k suffix if >= 1000."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def render_report_json(rows: list[dict]) -> dict:
    """Return structured stats as a JSON‑serialisable dict.

    The output mirrors ``render_report`` but is machine‑readable, containing:
    * ``total_sessions`` – number of recorded sessions
    * ``total_tokens`` – sum of ``peak_tokens`` across all rows
    * ``models`` – list from :func:`model_report`
    * ``stages`` – list from :func:`stage_report`
    * ``tasks`` – list from :func:`task_report`
    """
    total_sessions = len(rows)
    total_tokens = sum(int(r.get("peak_tokens", 0)) for r in rows)
    return {
        "total_sessions": total_sessions,
        "total_tokens": total_tokens,
        "models": model_report(rows),
        "stages": stage_report(rows),
        "tasks": task_report(rows),
    }
