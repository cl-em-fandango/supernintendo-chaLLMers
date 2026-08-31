"""Hand a session that crossed the context cap to a fresh session.

Crossing `maxPromptTokens` stops the streaming child (T48). Until now that stop
ended the task: `Pipeline._run` raised and `process` parked it. The stop is a
*warning*, not a verdict — the commits, artifacts and partial output the
session produced are all still valid work — so the pipeline now writes a
handover note and resumes the same stage in a clean session.

Two responsibilities, both here and nowhere else:

- `ContinuationNote` + `write_note`: what the stopped session leaves behind
  (why it stopped, where it stopped, and the text it managed to emit);
- `continuation_prompt`: the wrapper the resuming session is sent with, telling
  it to read the note and continue rather than start over.

Parking survives only as the exhaustion path, when
`maxContextContinuations` fresh sessions have all crossed the cap on the same
stage; that routing stays in `pipeline.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..core.enums import Stage
from .task_lifecycle import write_atomic

# How much of the stopped session's own text is copied into the note. The whole
# transcript is already on disk at `output_path`; the note carries enough for
# the next session to orient itself without spending its own budget re-reading.
PARTIAL_OUTPUT_CHARS = 4000


@dataclass
class ContinuationNote:
    """One handover, as the resuming session reads it.

    `attempt` is 1 for the first handover of a stage. `note_path` is filled in
    by `write_note`, which returns a new note rather than mutating this one, so
    a note object always describes a file that exists.
    """
    stage: Stage | str
    attempt: int
    peak_tokens: int
    context_limit: int | None = None
    slice_id: str | None = None
    iteration: int = 1
    output_path: Path | None = None
    note_path: Path | None = None


def _wire(value) -> str:
    """A `Stage` member's wire name; anything else its `str`.

    Deliberately local: the continuation note is written by `workflow/`, and
    importing the pipeline's helper here would point the dependency backwards.
    """
    return value.value if isinstance(value, Stage) else str(value)


def handover_dir(task_dir: Path | None, output_path: Path | None) -> Path:
    """Where a handover note belongs.

    A task's notes go in its `artifacts/progress/` dir beside the progress notes
    the stages already write. A session with no task (a bare run, as
    `autonomous.py` does) has no task dir, so its note joins the output file the
    harness already left there — a handover with nowhere to be written is worse
    than one written next to the work it describes.
    """
    if task_dir is not None:
        return task_dir / "artifacts" / "progress"
    return Path(output_path).parent if output_path else Path.cwd()


def note_path(notes_dir: Path, stage: Stage | str, slice_id: str | None,
              attempt: int) -> Path:
    """`<notes_dir>/handover-<stage>[-slice-<id>]-<n>.md`.

    Slice ids are used verbatim, the way `checkpointed_slices` stores them, so a
    note for slice `2.1` never collides with one for `2`.
    """
    name = f"handover-{_wire(stage)}"
    if slice_id:
        name += f"-slice-{slice_id}"
    return notes_dir / f"{name}-{attempt}.md"


def _note_text(note: ContinuationNote, partial_output: str) -> str:
    """The note body: the stop, the partial output, and what to do next."""
    text = partial_output.strip()
    if len(text) > PARTIAL_OUTPUT_CHARS:
        text = "… (earlier output truncated — see the partial output file)\n" \
               + text[-PARTIAL_OUTPUT_CHARS:]
    lines = [
        "# Handover: session stopped at the context cap",
        "",
        f"- stage: {_wire(note.stage)}",
        f"- slice: {note.slice_id if note.slice_id else 'none'}",
        f"- iteration: {note.iteration}",
        f"- continuation: {note.attempt}",
        f"- stopped at: {note.peak_tokens} tokens "
        f"(cap {note.context_limit if note.context_limit else 'unknown'})",
        f"- partial output: {note.output_path if note.output_path else 'none'}",
        "",
        "The session was stopped by the harness because its context crossed the",
        "configured cap. This is a budget warning, not a failed review: nothing it",
        "did was rejected.",
        "",
        "## What the stopped session said",
        "",
        text if text else "(no text was captured before the stop)",
        "",
        "## Next session should",
        "",
        "- Check `git status`, `git log` and the task artifacts to see what is",
        "  already done and committed before writing anything.",
        "- Continue from there. Do not redo completed work.",
        "- Stay inside your own context budget. If you cannot finish, write a",
        "  progress note and stop cleanly rather than pushing to the cap.",
        "",
    ]
    return "\n".join(lines)


def write_note(notes_dir: Path, note: ContinuationNote,
               partial_output: str) -> ContinuationNote:
    """Write one handover note and return the note carrying its path.

    Atomic (`write_atomic`), like every other harness artifact: a resuming
    session must never read a half-written handover.
    """
    path = note_path(notes_dir, note.stage, note.slice_id, note.attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, _note_text(note, partial_output))
    return replace(note, note_path=path)


def continuation_prompt(base_prompt: str, note: ContinuationNote) -> str:
    """The prompt for the fresh session that picks the work up.

    The original stage prompt is appended unchanged: the resuming session is
    doing the *same* job under the same protocol, so the stage's own verdict
    options and output rules must survive verbatim.
    """
    where = f" (slice {note.slice_id})" if note.slice_id else ""
    cap = note.context_limit if note.context_limit else "the configured cap"
    return f"""A previous session working on this exact task{where} was STOPPED by
the harness after crossing its context budget ({note.peak_tokens} tokens over
{cap}). That was a budget warning, not a rejection: its work stands, and this is
continuation {note.attempt} of the same stage.

Read the handover note FIRST: {note.note_path}

Then check `git status`, `git log` and the task artifacts to see what is already
done. Continue from where it stopped — do not redo completed work, and keep your
own context inside the budget.

--- the task, unchanged from the stopped session ---

{base_prompt}
"""
