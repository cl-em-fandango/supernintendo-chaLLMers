"""Persist one Markdown transcript per pi session under the task's artifacts.

A transcript is the auditable record of a single LLM interaction: the exact
prompt sent, the exact assistant output received, the child's stderr (when
non-empty), and the metadata an operator needs to place the session (stage,
model, duration, peak tokens, rc, verdict, crashed).

Files land in `<task_dir>/artifacts/sessions/` named
`NNN-<stage>[-slice-<id>][-iter-<n>].md`, where `NNN` is a per-task sequence
number derived from
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
    error: str = ""


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
    """`NNN-<stage>[-slice-<id>][-iter-<n>].md` for one transcript.

    The slice and iteration parts say at a glance which unit of work a
    transcript belongs to; `-iter-1` is omitted because it is the common case
    and the stats row carries the number either way. The `NNN` prefix is what
    guarantees uniqueness — two attempts of the same stage/slice/iteration
    (a crash retry reuses all three) still get one file each.
    """
    name = f"{record.sequence:03d}-{record.stage}"
    if record.slice_id is not None:
        name += f"-slice-{record.slice_id}"
    if record.iteration != 1:
        name += f"-iter-{record.iteration}"
    return f"{name}.md"


def has_stderr(record: TranscriptRecord) -> bool:
    """Whether this transcript gets a `## Stderr` section.

    The rule is 'non-empty stderr', plus one edge case the spec names: a
    session that came back with nothing at all (empty output, empty stderr,
    rc != 0) still has to read as a complete record, so a failed session gets
    the section even when it is empty. A *successful* session with nothing on
    stderr does not get an empty section.
    """
    return bool(record.stderr) or record.rc != 0 or record.crashed


def render_transcript(record: TranscriptRecord) -> str:
    """Render one transcript as Markdown (see module docstring for layout).

    Every section is fenced independently: the Prompt, Output and Stderr
    contents are unrelated, so one containing a ``` run cannot break another
    one's delimiter (see `_fenced`).
    """
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
    ]
    if record.error:
        # The run's failure text (e.g. `wall-clock timeout after 3600s`) is not
        # stderr, so it gets its own metadata line rather than being spliced
        # into the Stderr section. Collapsed to one line to keep the list flat.
        lines.append(f"- error: {' '.join(record.error.split())}")
    lines += [
        "",
        "## Prompt",
        "",
        _fenced(record.prompt),
        "",
        "## Output",
        "",
        _fenced(record.output),
    ]
    if has_stderr(record):
        lines += ["", "## Stderr", "", _fenced(record.stderr)]
    lines.append("")
    return "\n".join(lines)


def write_transcript(task_dir: Path, record: TranscriptRecord, log) -> Path | None:
    """Write one transcript file, warn-and-continue on failure.

    A transcript is an audit artifact, not pipeline state: a directory we
    cannot write to (read-only mount, disk full, permissions) must never abort
    the session that produced it. Same posture as the journey readout — the
    operator gets a warning line and the run continues.

    Returns the path written, or None when the write failed.
    """
    try:
        dest_dir = sessions_dir_for(task_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / transcript_filename(record)
        # `errors="replace"` is the last net for text that survived the stream
        # decode but cannot be encoded (lone surrogates from a broken child).
        path.write_text(render_transcript(record), encoding="utf-8",
                        errors="replace")
        return path
    except OSError as exc:
        log(f"  ! transcript write failed for session "
            f"{record.sequence:03d}-{record.stage}: {exc}")
        return None


def _fenced(content: str) -> str:
    """One fenced code block whose delimiter cannot appear inside `content`.

    The delimiter is a run of backticks strictly longer than the longest
    backtick run inside this block's own content, minimum three. Computed per
    block, never shared, so an output containing ``` is wrapped in ```` while
    a prompt without backticks keeps ```. An empty section still gets its
    fence pair — the header is emitted either way.
    """
    fence = "`" * (longest_backtick_run(content) + 1)
    if len(fence) < 3:
        fence = "```"
    return f"{fence}\n{content}\n{fence}"


def longest_backtick_run(content: str) -> int:
    """Length of the longest run of consecutive backticks in `content` (0 if none)."""
    longest = 0
    run = 0
    for ch in content:
        if ch == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest
