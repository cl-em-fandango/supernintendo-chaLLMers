"""Persist one Markdown transcript per pi session under the task's artifacts.

A transcript is the auditable record of a single LLM interaction: the exact
prompt sent, the exact assistant output received, the child's stderr (when
non-empty), and the metadata an operator needs to place the session (stage,
model, duration, peak tokens, rc, verdict, crashed).

Files land in `<task_dir>/artifacts/sessions/` named
`NNN-<stage>.md`, where `NNN` is a per-task sequence number derived from
`max(stats rows for the task, transcripts already on disk) + 1` so numbering
survives a process restart and never reuses a number already on disk.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class TranscriptRecord:
    """Everything one transcript file renders: identity, metadata, content.

    `sequence` is the `NNN` in the filename; `prompt` is the full string sent
    to the model (context-budget note included), not the stage prompt alone.
    """
    sequence: int
    task_id: str
    stage: str
    timestamp: str
    model: str
    duration_s: float
    peak_tokens: int
    rc: int
    verdict: str
    crashed: bool
    prompt: str
    output: str
    stderr: str
    slice_id: str | None = None
    iteration: int = 1


def resolve_task_dir(queue_dir: Path, task_id: str) -> Path | None:
    """Locate `<queue>/<sub>/<task_id>` across the queue subdirectories.

    The session layer only knows the task id; the task may sit in `active/`,
    `parked/` or any other state directory. Returns None when no directory
    matches, so a caller with a bare or unknown id degrades to no transcript
    instead of inventing a queue layout.
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return None
    for sub in sorted(queue_dir.iterdir()):
        candidate = sub / task_id
        if candidate.is_dir():
            return candidate
    return None


def sessions_dir_for(task_dir: Path) -> Path:
    """The directory holding this task's transcripts."""
    return Path(task_dir) / "artifacts" / "sessions"


def next_sequence(stats_row_count: int, task_dir: Path) -> int:
    """`NNN` for the next transcript of a task.

    Counts the sessions already recorded in the stats store *and* the
    transcripts already on disk, then adds one — the larger of the two wins,
    so a restored `artifacts/` directory can never be overwritten by a fresh
    run. Callers compute this before appending the current session's row so
    the first session of a task is `001`.
    """
    sessions_dir = sessions_dir_for(task_dir)
    on_disk = len(list(sessions_dir.glob("*.md"))) if sessions_dir.is_dir() else 0
    return max(stats_row_count, on_disk) + 1


def transcript_filename(record: TranscriptRecord) -> str:
    """`NNN-<stage>.md` for one transcript."""
    return f"{record.sequence:03d}-{record.stage}.md"


def render_transcript(record: TranscriptRecord) -> str:
    """Render one transcript as Markdown (see module docstring for layout)."""
    lines = [
        f"# Session {record.sequence:03d}: {record.stage} ({record.task_id})",
        "",
        f"- timestamp: {record.timestamp}",
        f"- stage: {record.stage}",
    ]
    if record.slice_id is not None:
        lines.append(f"- slice: {record.slice_id}")
    lines += [
        f"- iteration: {record.iteration}",
        f"- model: {record.model}",
        f"- duration_s: {record.duration_s:.1f}",
        f"- peak_tokens: {record.peak_tokens}",
        f"- rc: {record.rc}",
        f"- verdict: {record.verdict}",
        f"- crashed: {'true' if record.crashed else 'false'}",
        "",
        "## Prompt",
        "",
        _fenced(record.prompt),
        "",
        "## Output",
        "",
        _fenced(record.output),
    ]
    if record.stderr:
        lines += ["", "## Stderr", "", _fenced(record.stderr)]
    lines.append("")
    return "\n".join(lines)


def write_transcript(task_dir: Path, record: TranscriptRecord) -> Path:
    """Write one transcript file and return its path."""
    dest_dir = sessions_dir_for(task_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / transcript_filename(record)
    path.write_text(render_transcript(record))
    return path


def _fenced(content: str) -> str:
    """One fenced code block. Fence escaping is a later slice."""
    return f"```\n{content}\n```"
