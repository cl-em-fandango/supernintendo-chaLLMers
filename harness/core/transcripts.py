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

Sessions with no task association (the autonomous loop, `task_id=None`) have
no task dir and no journey to link from. They are recorded in a work-dir-level
pool, `<harnessExecutionAndQueueDir>/artifacts/sessions/`, in the same format, named with a
sortable UTC timestamp prefix instead of a task sequence number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_TRANSCRIPT_NAME = re.compile(
    r"^(?P<sequence>\d+)-(?P<stage>.+?)"
    r"(?:-slice-(?P<slice>.+?))?(?:-iter-(?P<iteration>\d+))?\.md$"
)


@dataclass
class TranscriptRecord:
    """Everything one transcript file renders: identity, metadata, content.

    `sequence` is the `NNN` in the filename; `prompt` is the full string sent
    to the model (context-budget note included), not the stage prompt alone.
    A pooled (task-less) transcript has no sequence number and no task id —
    its timestamp prefix is its only identity.
    """
    sequence: int | None
    task_id: str | None
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


@dataclass
class TranscriptFile:
    """One transcript already on disk, read back out of its filename.

    The filename is the only place the sequence number and the slice/iteration
    identity survive after the writer is done, so this is what the journey map
    uses to point a stats row at its transcript.
    """
    sequence: int
    stage: str
    slice_id: str | None
    iteration: int
    name: str
    path: Path


def parse_transcript_filename(path: Path) -> TranscriptFile | None:
    """Read `NNN-<stage>[-slice-<id>][-iter-<n>].md` back into its parts.

    Returns None for a name the writer would never produce, so a stray file in
    `sessions/` is ignored rather than matched to the wrong session.
    """
    match = _TRANSCRIPT_NAME.match(Path(path).name)
    if match is None:
        return None
    return TranscriptFile(
        sequence=int(match.group("sequence")),
        stage=match.group("stage"),
        slice_id=match.group("slice"),
        iteration=int(match.group("iteration") or 1),
        name=Path(path).name,
        path=Path(path),
    )


def list_transcripts(task_dir: Path) -> list[TranscriptFile]:
    """Every parseable transcript under `<task_dir>/artifacts/sessions/`.

    Sorted by sequence number, so a caller can pair them with sessions in
    chronological order. A missing sessions directory is simply no transcripts.
    """
    sessions_dir = sessions_dir_for(task_dir)
    if not sessions_dir.is_dir():
        return []
    parsed = [parse_transcript_filename(p) for p in sessions_dir.glob("*.md")]
    return sorted((t for t in parsed if t is not None),
                  key=lambda t: (t.sequence, t.name))


def match_rows_to_transcripts(rows: list[dict],
                              transcripts: list[TranscriptFile]) -> list[str | None]:
    """Pair each stats row with a transcript filename, positionally (None = none).

    Stats rows for a task are chronological and transcripts are numbered from
    the row count, so row *i* normally carries sequence *i*. A resumed task can
    start numbering past the restored row count, so the sequence is only a
    first candidate: what actually binds a pair is stage, slice and iteration.
    Each transcript is used at most once, and a row with no plausible partner
    gets None — the journey then shows `—` rather than a broken link.
    """
    pool = list(transcripts)
    used: set[str] = set()
    paired: list[str | None] = []
    for index, row in enumerate(rows, 1):
        found = _match_one(row, index, pool, used)
        paired.append(found.name if found else None)
        if found:
            used.add(found.name)
    return paired


def _match_one(row: dict, index: int, pool: list[TranscriptFile],
               used: set[str]) -> TranscriptFile | None:
    """The transcript for one row: sequence first, then identity in order."""
    for candidate in pool:
        if candidate.name not in used and candidate.sequence == index \
                and _identity_matches(row, candidate):
            return candidate
    for candidate in pool:
        if candidate.name not in used and _identity_matches(row, candidate):
            return candidate
    return None


def _identity_matches(row: dict, transcript: TranscriptFile) -> bool:
    """Whether a row and a transcript describe the same unit of work.

    Slice ids are strings on disk and may be recorded as ints in a row, so both
    sides are compared as strings; an absent slice is only `None` on both sides.
    """
    row_slice = row.get("slice")
    return (
        str(row.get("stage", "")) == transcript.stage
        and (None if row_slice is None else str(row_slice)) == transcript.slice_id
        and int(row.get("iteration", 1) or 1) == transcript.iteration
    )


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
    if record.sequence is not None:
        title = f"# Session {record.sequence:03d}: {record.stage} ({record.task_id})"
    else:
        title = f"# Session {record.stage} (pooled)"
    lines = [
        title,
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


def pooled_sessions_dir(work_dir: Path) -> Path:
    """The work-dir-level transcript pool for sessions with no task."""
    return Path(work_dir) / "artifacts" / "sessions"


def pooled_timestamp(now: datetime | None = None) -> str:
    """Sortable, collision-free UTC prefix for a pooled transcript name.

    ISO-8601 basic format with microseconds (`YYYYMMDDTHHMMSS.ffffffZ`), so
    lexicographic order is chronological order and two sessions recorded in
    the same wall-clock second still get distinct names.
    """
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%S.%fZ")


def pooled_transcript_filename(stage: str, timestamp: str) -> str:
    """`<timestamp>-<stage>.md` for one pooled transcript."""
    return f"{timestamp}-{stage}.md"


def write_pooled_transcript(work_dir: Path, record: TranscriptRecord,
                            log) -> Path | None:
    """Record a task-less session in the work-dir pool, warn-and-continue.

    Same audit posture as `write_transcript`: a failed pooled write must not
    abort the session that produced it, and per FR-5 C4 the fallback for a
    pooled session that cannot be recorded is the explicit skip-warning naming
    the stage and the reason — never silence.

    Returns the path written, or None when the write failed.
    """
    try:
        dest_dir = pooled_sessions_dir(work_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = _unique_pooled_path(dest_dir, record.stage)
        path.write_text(render_transcript(record), encoding="utf-8",
                        errors="replace")
        return path
    except OSError as exc:
        log(f"  ! no pooled transcript written for stage "
            f"{record.stage}: {exc}")
        return None


def _unique_pooled_path(dest_dir: Path, stage: str) -> Path:
    """A pooled filename that does not collide with one already on disk.

    Microsecond timestamps do not collide in practice; the `-<n>` suffix is a
    belt-and-braces net for a clock adjustment landing two writes on the same
    microsecond, so a pooled write can never overwrite its predecessor.
    """
    timestamp = pooled_timestamp()
    path = dest_dir / pooled_transcript_filename(stage, timestamp)
    n = 2
    while path.exists():
        path = dest_dir / f"{timestamp}-{stage}-{n}.md"
        n += 1
    return path


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
