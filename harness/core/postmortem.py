"""Post-mortem analysis of a stopped task: data model, analyzer, renderer.

Reconstructs what a parked/failed task achieved, where it stopped and why,
strictly read-only (spec §3/§4). This module owns the `PostMortem` shape, the
`PostMortemAnalyzer` that gathers the input sources, and
`render_post_mortem_markdown` that renders the report.

Slice 1 added the vertical spine (task resolution, minimal header); slice 2
added the failure-mode taxonomy, per-session signal detection over
`sessions.jsonl` rows plus transcripts, and the `## Point of failure`
section. Slice 3 completes the classification (spec §5 rules 3–4: the
review-summary park reason, then UNKNOWN), renders the `## What happened`
narrative (including the recorded park reason), and handles the `active/`
progress-snapshot note. Slice 4 adds the last-successful-checkpoint block
(spec §6): the header line, the telemetry cross-check with its disagreement
note, the `### Accomplished` bullets and the resume-readiness line. The
remaining report sections are added in later slices (spec §7–§8).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .config import DEFAULT_SESSION_TIMEOUT_S, Config
from .enums import CHECKPOINT_ORDER, CheckpointStage, Stage, TaskStatus, Verdict
from .stats import StatsStore
from .transcripts import (
    TranscriptFile,
    list_transcripts,
    match_rows_to_transcripts,
    resolve_task_dir,
)
from ..workflow.task_lifecycle import TaskLifecycle, TaskState


@dataclass
class PostMortemParams:
    """The inputs a `PostMortemAnalyzer` is given by injection (spec §11).

    `queue_dir` and `stats_path` are the two read roots; the config values
    (`session_timeout_s`) parameterize the Suggestions section and are
    carried from the start so later slices need no new plumbing.
    """
    queue_dir: Path
    stats_path: Path
    session_timeout_s: int = DEFAULT_SESSION_TIMEOUT_S


class PostMortemFailureMode(str, Enum):
    """The classified cause of a task's stop (spec §5).

    Values are the wire strings printed in the report; the report renders
    `label (value)`, so the label lives in `FAILURE_MODE_LABELS`.
    """
    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
    CONTEXT_BUDGET = "context_budget"
    CRASH = "crash"
    MODEL_REJECTION = "model_rejection"
    ERROR_OTHER = "error_other"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


FAILURE_MODE_LABELS: dict[PostMortemFailureMode, str] = {
    PostMortemFailureMode.WALL_CLOCK_TIMEOUT: "Wall-clock timeout",
    PostMortemFailureMode.CONTEXT_BUDGET: "Context budget exhaustion",
    PostMortemFailureMode.CRASH: "Subprocess crash",
    PostMortemFailureMode.MODEL_REJECTION: "Model rejection loop",
    PostMortemFailureMode.ERROR_OTHER: "Error, unclassified signal",
    PostMortemFailureMode.COMPLETED: "Completed",
    PostMortemFailureMode.UNKNOWN: "Unknown",
}

# Verdicts/outcomes that mean "the model bounced the work" (spec §5
# Rejection rule). Wire strings appear only inside this enum-adjacent set.
_REJECTION_VERDICTS = frozenset(
    {"kickback", "fail", "kickout", "reject", "infeasible"})
_REJECTION_OUTCOMES = frozenset({"kickback", "fail", "kickout", "error"})

# Within one classifying session, a resource stop is the root cause and a
# rejection verdict is often its symptom (spec §5 rule 2).
_MODE_BY_SIGNAL_PRIORITY = (
    PostMortemFailureMode.WALL_CLOCK_TIMEOUT,
    PostMortemFailureMode.CONTEXT_BUDGET,
    PostMortemFailureMode.CRASH,
    PostMortemFailureMode.ERROR_OTHER,
    PostMortemFailureMode.MODEL_REJECTION,
)

# How many of the most recent sessions the classification scan covers.
CLASSIFY_SCAN_WINDOW = 3

# Printed under the header for a task that is still in `active/` (spec §9).
ACTIVE_TASK_NOTE = ("Task appears still in flight; this is a progress "
                    "snapshot, not a post-mortem.")

# Park/fail reason wordings -> mode (spec §5 rule 3). The reason text is the
# exact string passed to `lifecycle.park`/`fail`, persisted only in the
# review summary; the regexes match the observed phrasings loosely so a
# reworded park still classifies.
_REASON_MODE_PATTERNS: tuple[tuple[re.Pattern, PostMortemFailureMode], ...] = (
    (re.compile(r"timeout|timed out"),
     PostMortemFailureMode.WALL_CLOCK_TIMEOUT),
    (re.compile(r"over context|over-cap"),
     PostMortemFailureMode.CONTEXT_BUDGET),
    (re.compile(r"crash"), PostMortemFailureMode.CRASH),
    (re.compile(r"loop exceeded|still failing|not delivered in \d+"
                r"|review after \d+"),
     PostMortemFailureMode.MODEL_REJECTION),
)

# Evidence quotes are truncated to this many characters (spec §7/§9).
EVIDENCE_MAX_CHARS = 200

# Verdicts that mark a telemetry row as "this session's work passed" — the
# checkpoint cross-check of spec §6.
_PASSING_VERDICTS = frozenset({Verdict.PASS, Verdict.DONE, Verdict.RESLICED,
                               Verdict.PROGRESS})

# Pipeline stage (the vocabulary of telemetry rows) -> the checkpoint stage
# its passing session implies (the vocabulary of `checkpointed_stages`).
_PIPELINE_STAGE_TO_CHECKPOINT = {
    Stage.SPEC_AUTHOR: CheckpointStage.SPEC,
    Stage.SPEC_ASSESS_TW: CheckpointStage.SPEC,
    Stage.SPEC_ASSESS_ORNITH: CheckpointStage.SPEC,
    Stage.FEASIBILITY: CheckpointStage.FEASIBILITY,
    Stage.SLICING: CheckpointStage.SLICING,
    Stage.SLICE_CHECK: CheckpointStage.SLICES,
    Stage.SLICE_IMPLEMENT: CheckpointStage.SLICES,
    Stage.SLICE_FIX: CheckpointStage.SLICES,
    Stage.TECH_REVIEW: CheckpointStage.SLICES,
    Stage.FUNC_REVIEW: CheckpointStage.SLICES,
    Stage.HOLISTIC: CheckpointStage.MERGE,
    Stage.AUTONOMOUS_SUGGEST: CheckpointStage.MERGE,
    Stage.AUTONOMOUS_REVIEW: CheckpointStage.MERGE,
}

# Printed when the state file and the telemetry describe different
# checkpoints (spec §6).
DISAGREEMENT_NOTE = ("state file and telemetry disagree; the state file "
                     "is authoritative for resume")

# Printed when `task.json` exists but cannot be parsed (spec §9).
UNREADABLE_STATE_NOTE = ("task.json unreadable; checkpoints inferred from "
                         "telemetry")

# Reported when neither the state file nor the telemetry shows any
# completed stage (spec §6).
NO_CHECKPOINT_TEXT = ("No stage checkpointed — the task failed before the "
                      "spec stage completed.")

_TIMEOUT_MARKERS = ("timed out", "timeout")
_CRASH_NOTE_MARKER = "[crashed:"
_CONTEXT_NOTE_MARKER = "over-cap"
_CONTEXT_TRANSCRIPT_MARKER = "over context cap"
_TRANSCRIPT_ERROR_LINE = re.compile(r"^\s*-\s*error:\s*(.*)$")
_TRANSCRIPT_CRASHED_LINE = re.compile(r"^\s*-\s*crashed:\s*(\S+)\s*$")


@dataclass
class TranscriptDiagnostics:
    """The two metadata lines of a transcript the classifier reads.

    Only the header is parsed, so a multi-megabyte prompt/output body is
    never held for a post-mortem.
    """
    error: str = ""
    crashed: bool = False


@dataclass
class SessionSignals:
    """The failure signals one telemetry row (with its transcript) shows.

    Each flag carries its own raw evidence string (the notes/error text or
    the reconstructed `verdict=… outcome=… rc=…`), so the renderer can show
    exactly what a rule matched. `is_empty()` means the session carries no
    signal at all.
    """
    timeout: str = ""
    context_budget: str = ""
    crash: str = ""
    error_other: str = ""
    rejection: str = ""

    def is_empty(self) -> bool:
        return not (self.timeout or self.context_budget or self.crash
                    or self.error_other or self.rejection)

    def classify(self) -> PostMortemFailureMode:
        """The mode for this one session, by the spec §5 priority order."""
        flags = {
            PostMortemFailureMode.WALL_CLOCK_TIMEOUT: self.timeout,
            PostMortemFailureMode.CONTEXT_BUDGET: self.context_budget,
            PostMortemFailureMode.CRASH: self.crash,
            PostMortemFailureMode.ERROR_OTHER: self.error_other,
            PostMortemFailureMode.MODEL_REJECTION: self.rejection,
        }
        for mode in _MODE_BY_SIGNAL_PRIORITY:
            if flags[mode]:
                return mode
        return PostMortemFailureMode.UNKNOWN

    def evidence_for(self, mode: PostMortemFailureMode) -> str:
        """The raw evidence string that produced `mode` for this session."""
        return {
            PostMortemFailureMode.WALL_CLOCK_TIMEOUT: self.timeout,
            PostMortemFailureMode.CONTEXT_BUDGET: self.context_budget,
            PostMortemFailureMode.CRASH: self.crash,
            PostMortemFailureMode.ERROR_OTHER: self.error_other,
            PostMortemFailureMode.MODEL_REJECTION: self.rejection,
        }.get(mode, "")


@dataclass
class ClassifiedFailure:
    """The task-level failure verdict.

    `session_index` is the 1-based position of the classifying session in
    `StatsStore.for_task` order (same numbering as the journey); None when
    no session classified the task (UNKNOWN, and COMPLETED).
    """
    mode: PostMortemFailureMode
    session_index: int | None = None
    row: dict | None = None
    evidence: str = ""
    transcript_path: Path | None = None


@dataclass
class ReviewSummary:
    """The park/fail reason recorded in `<queue>/review/<task_id>.md`.

    `reason` is the Executive summary body the pipeline wrote at its final
    terminal move; empty when no review summary (or no such section) exists.
    """
    path: Path
    reason: str = ""


def read_review_summary(queue_dir: Path, task_id: str) -> ReviewSummary | None:
    """Read the task's review summary, or None when there is no file.

    Missing or unreadable files degrade to None — the narrative then omits
    the recorded reason and classification falls through the §5 rules.
    """
    path = queue_dir / "review" / f"{task_id}.md"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return ReviewSummary(path=path, reason=_exec_summary_body(text))


def _exec_summary_body(text: str) -> str:
    """The body of the `## Executive summary` section, stripped."""
    body: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip() == "## Executive summary"
            continue
        if in_section:
            body.append(line)
    return "\n".join(body).strip()


def classify_from_reason(reason: str) -> PostMortemFailureMode | None:
    """Map a park/fail reason string to a mode (spec §5 rule 3).

    Returns None when no known wording matches; the caller then falls
    through to UNKNOWN.
    """
    lowered = reason.lower()
    for pattern, mode in _REASON_MODE_PATTERNS:
        if pattern.search(lowered):
            return mode
    return None


@dataclass
class TelemetryCheckpoint:
    """The latest passing telemetry row, read as a checkpoint cross-check.

    `session_index` is the 1-based position in `StatsStore.for_task` order
    (the same numbering as the journey and the Point-of-failure section).
    `slice_id` is empty for rows of stages that carry no slice.
    """
    session_index: int
    stage: str
    slice_id: str = ""


@dataclass
class CheckpointReport:
    """The last successful checkpoint and what a resume would do (spec §6).

    `stages`/`slices` come from the state file (authoritative); `telemetry`
    is the cross-check from the latest passing row. `disagree` means the
    telemetry checkpoint is not covered by the state file. `unreadable`
    means `task.json` exists but does not parse, so `stages`/`slices` are
    empty and only the telemetry cross-check remains. `resume_stage` and
    `resume_slice` state what a `harness.py resume` would restart from.
    """
    stages: list[CheckpointStage] = field(default_factory=list)
    slices: list[str] = field(default_factory=list)
    telemetry: TelemetryCheckpoint | None = None
    disagree: bool = False
    unreadable: bool = False
    resume_stage: CheckpointStage | None = None
    resume_slice: str | None = None


def build_checkpoint_report(state: TaskState | None,
                            rows: list[dict],
                            unreadable: bool = False) -> CheckpointReport:
    """Assemble the checkpoint block from `task.json` state and telemetry.

    The state file is authoritative; the telemetry row is a cross-check.
    A missing state (`state is None`) yields empty checkpoints, and the
    resume point then names the first stage of the pipeline.
    """
    stages = _ordered_checkpoint_stages(
        state.checkpointed_stages if state is not None else [])
    slices = [str(slice_id) for slice_id
              in (state.checkpointed_slices if state is not None else [])]
    telemetry = _latest_passing_checkpoint(rows)
    return CheckpointReport(
        stages=stages,
        slices=slices,
        telemetry=telemetry,
        disagree=(telemetry is not None
                  and not _telemetry_agrees(telemetry, stages, slices)),
        unreadable=unreadable,
        resume_stage=_resume_stage(stages, slices),
        resume_slice=_resume_slice(stages, slices),
    )


def _ordered_checkpoint_stages(raw: list) -> list[CheckpointStage]:
    """Normalize checkpointed stages to deduped CHECKPOINT_ORDER members."""
    stages: list[CheckpointStage] = []
    for entry in raw:
        if isinstance(entry, CheckpointStage):
            stage = entry
        else:
            try:
                stage = CheckpointStage(entry)
            except ValueError:
                continue
        if stage not in stages:
            stages.append(stage)
    return sorted(stages, key=CHECKPOINT_ORDER.index)


def _latest_passing_checkpoint(rows: list[dict]) -> TelemetryCheckpoint | None:
    """The last row whose verdict says the session's work passed."""
    for index in range(len(rows), 0, -1):
        row = rows[index - 1]
        verdict = Verdict.parse(str(row.get("verdict") or ""))
        if verdict in _PASSING_VERDICTS:
            return TelemetryCheckpoint(
                session_index=index,
                stage=str(row.get("stage") or "unknown"),
                slice_id=str(row.get("slice") or ""),
            )
    return None


def _telemetry_agrees(telemetry: TelemetryCheckpoint,
                      stages: list[CheckpointStage],
                      slices: list[str]) -> bool:
    """Whether the telemetry checkpoint is covered by the state file.

    A row that names a slice agrees when that slice is checkpointed; a
    slice-less row agrees when its pipeline stage maps to a checkpointed
    stage. Anything else means the state file does not record what the
    telemetry says passed — a disagreement worth flagging.
    """
    if telemetry.slice_id:
        return telemetry.slice_id in slices
    checkpoint = _PIPELINE_STAGE_TO_CHECKPOINT.get(Stage.parse(telemetry.stage))
    return checkpoint is not None and checkpoint in stages


def _resume_stage(stages: list[CheckpointStage],
                  slices: list[str]) -> CheckpointStage | None:
    """The stage a resume restarts from: completed slices mean `slices`.

    Completed slice ids imply the slicing stage passed and the task stops
    mid-`slices`; otherwise it is the first stage of CHECKPOINT_ORDER the
    state file does not list. None means every stage is checkpointed.
    """
    if slices:
        return CheckpointStage.SLICES
    for stage in CHECKPOINT_ORDER:
        if stage not in stages:
            return stage
    return None


def _resume_slice(stages: list[CheckpointStage],
                  slices: list[str]) -> str | None:
    """The next slice id a resume would take: the lowest unused number."""
    if not slices:
        return None
    used: set[int] = set()
    for slice_id in slices:
        try:
            used.add(int(slice_id))
        except ValueError:
            continue
    candidate = 1
    while candidate in used:
        candidate += 1
    return str(candidate)


@dataclass
class PostMortemNarrative:
    """The facts behind the `## What happened` sentences (2–5 of them).

    Every field is observed data, never an inference: an empty field simply
    drops its sentence from the narrative rather than fabricating a value.
    """
    reached_stages: list[str] = field(default_factory=list)
    completed_slices: list[str] = field(default_factory=list)
    session_count: int = 0
    last_stage: str = ""
    last_stage_iterations: int = 1
    stop_session_index: int | None = None
    stop_stage: str = ""
    stop_date: str = ""
    stop_verdict: str = ""
    stop_outcome: str = ""
    park_reason: str = ""


def _stage_text(stage) -> str:
    """The wire string of a checkpoint stage (enum member or plain str)."""
    return stage.value if isinstance(stage, Enum) else str(stage)


def build_narrative(report: "PostMortem") -> PostMortemNarrative:
    """Collect the narrative facts from checkpoints, rows and the review."""
    narrative = PostMortemNarrative(session_count=len(report.rows))
    if report.state is not None:
        narrative.reached_stages = [_stage_text(stage)
                                    for stage in report.state.checkpointed_stages]
        narrative.completed_slices = [str(slice_id)
                                      for slice_id
                                      in report.state.checkpointed_slices]
    reason = (report.review.reason if report.review is not None else "")
    narrative.park_reason = " ".join(reason.split())
    if not report.rows:
        return narrative
    last_row = report.rows[-1]
    narrative.last_stage = str(last_row.get("stage") or "unknown")
    narrative.last_stage_iterations = max(
        (_row_iteration(row)
         for row in report.rows
         if str(row.get("stage") or "unknown") == narrative.last_stage),
        default=1)
    narrative.stop_session_index = len(report.rows)
    narrative.stop_stage = narrative.last_stage
    narrative.stop_date = str(last_row.get("ts") or "").split("T")[0]
    narrative.stop_verdict = str(last_row.get("verdict") or "")
    narrative.stop_outcome = str(last_row.get("outcome") or "")
    return narrative


def narrative_sentences(narrative: PostMortemNarrative) -> list[str]:
    """The 2–5 `## What happened` sentences, in order (spec §7)."""
    sentences: list[str] = []
    if narrative.reached_stages:
        reached = f"The task reached stage `{narrative.reached_stages[-1]}`"
        if narrative.completed_slices:
            reached += (
                f" with {len(narrative.completed_slices)} slice(s) "
                f"completed ({', '.join(narrative.completed_slices)})")
        sentences.append(reached + ".")
    if narrative.session_count:
        attempted = (f"It attempted {narrative.session_count} session(s), "
                     f"the last one at stage `{narrative.last_stage}`")
        if narrative.last_stage_iterations > 1:
            attempted += (f" over {narrative.last_stage_iterations} "
                          f"iteration(s)")
        sentences.append(attempted + ".")
    else:
        sentences.append("No sessions were recorded for this task; it "
                         "stopped before the first session started.")
    if narrative.stop_session_index is not None:
        stopped = (f"The task stopped at session "
                   f"#{narrative.stop_session_index} on "
                   f"{narrative.stop_date or 'an unknown date'} with "
                   f"verdict `{narrative.stop_verdict or '—'}` and outcome "
                   f"`{narrative.stop_outcome or '—'}`.")
        sentences.append(stopped)
    if narrative.park_reason:
        sentences.append(
            f'Reason recorded at park: "{narrative.park_reason}".')
    return sentences


@dataclass
class PostMortem:
    """The shape of one finished post-mortem analysis.

    `status` is None when no task directory was found (the report then says
    so); `state` is None when `task.json` is absent. `rows` are the task's
    telemetry rows in chronological order, straight from
    `StatsStore.for_task` (wire dicts, handled only at the edges).
    `transcript_paths` pairs each row (same order) with its matched
    transcript path, or None when the row has no transcript on disk.
    `checkpoint` is the last-successful-checkpoint block (spec §6).
    """
    task_id: str
    status: TaskStatus | None
    task_dir: Path | None
    state: TaskState | None
    rows: list[dict] = field(default_factory=list)
    transcript_paths: list[Path | None] = field(default_factory=list)
    review: ReviewSummary | None = None
    failure: ClassifiedFailure = field(
        default_factory=lambda: ClassifiedFailure(
            mode=PostMortemFailureMode.UNKNOWN))
    checkpoint: CheckpointReport = field(default_factory=CheckpointReport)


class PostMortemAnalyzer:
    """Gathers the spec §4 input sources for one task, read-only.

    Returns None from `analyze` when the task is nowhere: no directory in
    any queue subdirectory and no rows in `sessions.jsonl` (spec §3 exit 1).
    """

    def __init__(self, params: PostMortemParams, log=None):
        self.params = params
        self.log = log if log is not None else (lambda msg: None)

    def analyze(self, task_id: str) -> PostMortem | None:
        """Resolve the task across the queue and collect its inputs."""
        task_dir = resolve_task_dir(self.params.queue_dir, task_id)
        rows = StatsStore(self.params.stats_path).for_task(task_id)
        if task_dir is None and not rows:
            return None
        state, state_unreadable = self._load_state(task_id, task_dir)
        status = _status_of(task_dir, state)
        transcript_paths = self._match_transcripts(task_dir, rows)
        review = read_review_summary(self.params.queue_dir, task_id)
        failure = classify_failure(
            status=status,
            rows=rows,
            diagnostics_for=lambda index: _diagnostics_for_row(
                transcript_paths[index - 1]),
            park_reason=(review.reason if review is not None else ""),
        )
        if failure.session_index is not None:
            failure.transcript_path = transcript_paths[
                failure.session_index - 1]
        return PostMortem(
            task_id=task_id,
            status=status,
            task_dir=task_dir,
            state=state,
            rows=rows,
            transcript_paths=transcript_paths,
            review=review,
            failure=failure,
            checkpoint=build_checkpoint_report(
                state, rows, unreadable=state_unreadable),
        )

    def _match_transcripts(self, task_dir: Path | None,
                           rows: list[dict]) -> list[Path | None]:
        """Row-ordered transcript paths via the shared journey matcher."""
        if task_dir is None or not rows:
            return [None] * len(rows)
        transcripts = list_transcripts(task_dir)
        by_name: dict[str, TranscriptFile] = {
            t.name: t for t in transcripts}
        return [by_name[name].path if name else None
                for name in match_rows_to_transcripts(rows, transcripts)]

    def _load_state(self, task_id: str,
                    task_dir: Path | None) -> tuple[TaskState | None, bool]:
        """Read `task.json` via `TaskLifecycle.load_state`.

        Returns the state (None when absent or unparseable) and whether
        the file exists but does not hold a JSON object — the corrupt
        `task.json` case of spec §9, where checkpoints fall back to the
        telemetry alone.
        """
        if task_dir is None or not (task_dir / "task.json").exists():
            return None, False
        if not _task_json_is_object(task_dir / "task.json"):
            return None, True
        lifecycle = TaskLifecycle(self._lifecycle_config(), log=self.log)
        return lifecycle.load_state(task_id, where=task_dir.parent.name), False

    def _lifecycle_config(self) -> Config:
        """A Config whose `queue_dir` is the injected one.

        `load_state` only reads `cfg.queue_dir`, and the queue layout is
        `<work>/queue` by construction, so the parent reconstructs the
        work dir without the analyzer taking the whole harness config.
        """
        return Config(harness_execution_and_queue_dir=self.params.queue_dir.parent)


def _task_json_is_object(path: Path) -> bool:
    """Whether `path` parses as a JSON object (the `load_state` contract).

    `load_state` silently zeroes a corrupt file's checkpoints, which the
    report must distinguish from a genuinely checkpoint-free task; this
    probe is what lets the analyzer print the `task.json unreadable` note.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(raw, dict)


def _status_of(task_dir: Path | None,
               state: TaskState | None) -> TaskStatus | None:
    """The task's lifecycle status: `task.json` first, directory as fallback.

    A status string outside `TaskStatus` (a hand-made layout, a `review/`
    or `claimed/` directory) yields None; the renderer prints `unknown`.
    """
    raw = state.status if state is not None else (
        task_dir.parent.name if task_dir is not None else None)
    try:
        return TaskStatus(raw)
    except ValueError:
        return None


def detect_session_signals(row: dict,
                           diagnostics: TranscriptDiagnostics | None = None,
                           ) -> SessionSignals:
    """Apply the spec §5 per-session detection rules to one telemetry row.

    `diagnostics` is the matched transcript's `- error:` / `- crashed:`
    metadata (None when the row has no transcript). Matching is on the
    substrings the writers actually emit: `over-cap` appears only in the
    stats notes, `over context cap` only in the transcript error line.
    """
    notes = str(row.get("notes") or "")
    error_line = diagnostics.error if diagnostics else ""
    signals = SessionSignals()
    haystack = f"{notes}\n{error_line}".lower()

    for marker in _TIMEOUT_MARKERS:
        if marker in haystack:
            signals.timeout = _line_containing(notes, error_line, marker)
            break
    if _CONTEXT_NOTE_MARKER in notes:
        signals.context_budget = _line_containing(notes, error_line,
                                                  _CONTEXT_NOTE_MARKER)
    elif error_line and (_CONTEXT_TRANSCRIPT_MARKER in error_line.lower()
                         or _CONTEXT_NOTE_MARKER in error_line.lower()):
        signals.context_budget = error_line

    if _CRASH_NOTE_MARKER in notes:
        signals.crash = _line_containing(notes, error_line,
                                         _CRASH_NOTE_MARKER)
    elif diagnostics is not None and diagnostics.crashed:
        signals.crash = "crashed: true"

    verdict = str(row.get("verdict") or "").lower()
    outcome = str(row.get("outcome") or "").lower()
    if verdict in _REJECTION_VERDICTS or outcome in _REJECTION_OUTCOMES:
        signals.rejection = f"verdict={verdict or '—'} outcome={outcome or '—'}"

    rc = _row_rc(row)
    if outcome == "error" or rc != 0:
        signals.error_other = f"rc={rc} outcome={outcome or '—'}"
    return signals


def classify_failure(status: TaskStatus | None,
                     rows: list[dict],
                     diagnostics_for=lambda index: None,
                     park_reason: str = "",
                     ) -> ClassifiedFailure:
    """Classify the task-level failure mode (spec §5 rules 1–4).

    Rule 1: a `done` task is COMPLETED. A task still in `active/` is a
    progress snapshot, so its mode is forced UNKNOWN (spec §9). Rule 2:
    scan the last `CLASSIFY_SCAN_WINDOW` sessions most recent first — the
    first session carrying any signal classifies the task (recency wins
    across sessions); within that session the signal priority decides.
    Rule 3: with no signal in the window, classify from the review
    summary's park reason (`park_reason`). Rule 4: otherwise UNKNOWN.

    `diagnostics_for(index)` returns the row's `TranscriptDiagnostics` or
    None; `index` is the 1-based row position.
    """
    if status is TaskStatus.DONE:
        return ClassifiedFailure(mode=PostMortemFailureMode.COMPLETED)
    if status is TaskStatus.ACTIVE:
        return ClassifiedFailure(mode=PostMortemFailureMode.UNKNOWN)
    first = max(1, len(rows) - CLASSIFY_SCAN_WINDOW + 1)
    for index in range(len(rows), first - 1, -1):
        row = rows[index - 1]
        signals = detect_session_signals(row, diagnostics_for(index))
        if signals.is_empty():
            continue
        mode = signals.classify()
        return ClassifiedFailure(
            mode=mode,
            session_index=index,
            row=row,
            evidence=signals.evidence_for(mode),
        )
    if park_reason:
        mode = classify_from_reason(park_reason)
        if mode is not None:
            return ClassifiedFailure(
                mode=mode,
                evidence=" ".join(park_reason.split()),
            )
    return ClassifiedFailure(mode=PostMortemFailureMode.UNKNOWN)


def read_transcript_diagnostics(path: Path | None) -> TranscriptDiagnostics | None:
    """Parse a transcript's `- error:` / `- crashed:` header lines.

    Stops at the first `## ` section so the (potentially huge) prompt and
    output bodies are not read. An unreadable file is treated as no
    diagnostics rather than an error — the report degrades, never raises.
    """
    if path is None:
        return None
    diagnostics = TranscriptDiagnostics()
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("## "):
                    break
                error_match = _TRANSCRIPT_ERROR_LINE.match(line)
                if error_match:
                    diagnostics.error = error_match.group(1).strip()
                    continue
                crashed_match = _TRANSCRIPT_CRASHED_LINE.match(line)
                if crashed_match:
                    diagnostics.crashed = (crashed_match.group(1).lower()
                                           == "true")
    except OSError:
        return None
    return diagnostics if (diagnostics.error or diagnostics.crashed) else None


def _diagnostics_for_row(path: Path | None) -> TranscriptDiagnostics | None:
    """Diagnostics for one matched transcript path (None when unpaired)."""
    return read_transcript_diagnostics(path)


def _line_containing(notes: str, error_line: str, marker: str) -> str:
    """The rawest string holding `marker` (case-insensitively): notes first."""
    if marker in notes.lower():
        return notes
    return error_line or notes


def _row_iteration(row: dict) -> int:
    """The row's iteration as an int; anything unparseable counts as 1."""
    try:
        return int(row.get("iteration") or 1)
    except (TypeError, ValueError):
        return 1


def _row_rc(row: dict) -> int:
    """The row's return code as an int; anything unparseable counts as 0."""
    try:
        return int(row.get("rc") or 0)
    except (TypeError, ValueError):
        return 0


def render_post_mortem_markdown(report: PostMortem) -> str:
    """Render the post-mortem report as Markdown (deterministic, no clock).

    Slice 3 adds the active-task note and the `## What happened` narrative;
    slice 4 adds the checkpoint header block and `## State of play` /
    `### Accomplished`; later slices append the remaining sections in the
    spec §7 order.
    """
    status_text = report.status.value if report.status is not None else "unknown"
    failure = report.failure
    mode_text = (f"{FAILURE_MODE_LABELS[failure.mode]} "
                 f"(`{failure.mode.value}`)")
    lines = [
        f"# Post-mortem: {report.task_id}",
        "",
        f"**Status:** {status_text}",
        f"**Failure mode:** {mode_text}",
    ]
    lines += _render_checkpoint(report)
    if report.status is TaskStatus.ACTIVE:
        lines += ["", f"> {ACTIVE_TASK_NOTE}"]
    lines += _render_what_happened(report)
    lines += _render_state_of_play(report.checkpoint)
    lines += _render_point_of_failure(failure)
    return "\n".join(lines) + "\n"


def _render_checkpoint(report: "PostMortem") -> list[str]:
    """The header block: checkpoint line, cross-check, notes, resume line."""
    cp = report.checkpoint
    lines = [f"**Last successful checkpoint:** {_checkpoint_headline(cp)}"]
    if cp.telemetry is not None and cp.disagree:
        lines.append(f"**Telemetry cross-check:** "
                     f"{_telemetry_checkpoint_text(cp.telemetry)}")
        lines.append(f"! {DISAGREEMENT_NOTE}")
    if cp.unreadable:
        lines.append(f"! {UNREADABLE_STATE_NOTE}")
    lines.append(_resume_line(report.task_id, cp))
    return lines


def _checkpoint_headline(cp: CheckpointReport) -> str:
    """`<stage>[ / slices …]` from the state file, telemetry as fallback."""
    if cp.stages:
        headline = cp.stages[-1].value
        if cp.slices:
            headline += " / slices " + ", ".join(cp.slices)
        return headline
    if cp.slices:
        return "slices " + ", ".join(cp.slices)
    if cp.telemetry is not None:
        return (f"{_telemetry_checkpoint_text(cp.telemetry)} "
                f"(from telemetry alone)")
    return NO_CHECKPOINT_TEXT


def _telemetry_checkpoint_text(telemetry: TelemetryCheckpoint) -> str:
    """`session #N, stage `X`[ slice `Y`]` for the cross-check line."""
    text = (f"session #{telemetry.session_index}, stage "
            f"`{telemetry.stage}`")
    if telemetry.slice_id:
        text += f" slice `{telemetry.slice_id}`"
    return text


def _resume_line(task_id: str, cp: CheckpointReport) -> str:
    """The resume-readiness sentence (spec §6)."""
    if cp.resume_stage is None:
        return (f"A `harness.py resume {task_id}` would re-run the "
                f"terminal stage — every stage is checkpointed.")
    text = (f"A `harness.py resume {task_id}` would restart at stage "
            f"`{cp.resume_stage.value}`")
    if cp.resume_slice is not None:
        text += f", slice `{cp.resume_slice}`"
    return text + "."


def _render_state_of_play(cp: CheckpointReport) -> list[str]:
    """`## State of play` with the `### Accomplished` bullets."""
    lines = ["", "## State of play", "### Accomplished"]
    if cp.stages or cp.slices:
        lines += [f"- Stage `{stage.value}` checkpointed" for stage in cp.stages]
        lines += [f"- Slice `{slice_id}` completed" for slice_id in cp.slices]
    elif cp.telemetry is not None:
        lines.append(f"- {_telemetry_checkpoint_text(cp.telemetry)} "
                     f"(state file records no checkpoint)")
    else:
        lines.append(f"- {NO_CHECKPOINT_TEXT}")
    return lines


def _render_what_happened(report: PostMortem) -> list[str]:
    """The `## What happened` section: the narrative as one paragraph."""
    sentences = narrative_sentences(build_narrative(report))
    return ["", "## What happened", " ".join(sentences)]


def _render_point_of_failure(failure: ClassifiedFailure) -> list[str]:
    """The `## Point of failure` section, or nothing when unattributed."""
    if failure.session_index is None or failure.row is None:
        return []
    lines = [
        "",
        "## Point of failure",
        f"- Session #{_point_of_failure_head(failure)}",
        f"- Evidence: `{_truncate_evidence(failure.evidence)}`",
        f"- Transcript: {_transcript_text(failure.transcript_path)}",
    ]
    return lines


def _point_of_failure_head(failure: ClassifiedFailure) -> str:
    """`#K — <stage>[ slice <id>][ (iter N)]>, model <m>, <dur>s, peak <n>
    tokens, rc <rc>` for the classifying session."""
    row = failure.row
    stage = str(row.get("stage") or "unknown")
    slice_id = row.get("slice")
    if slice_id is not None:
        stage += f" slice {slice_id}"
    iteration = _row_iteration(row)
    if iteration != 1:
        stage += f" (iter {iteration})"
    model = str(row.get("model") or "unknown")
    duration = float(row.get("duration_s") or 0.0)
    peak_tokens = int(row.get("peak_tokens") or 0)
    rc = _row_rc(row)
    return (f"{failure.session_index} — {stage}, model {model}, "
            f"{duration:g}s, peak {peak_tokens} tokens, rc {rc}")


def _transcript_text(path: Path | None) -> str:
    """The transcript line: an absolute path, or the no-transcript text."""
    return str(path) if path is not None else "no transcript on disk"


def _truncate_evidence(text: str) -> str:
    """Quote evidence up to `EVIDENCE_MAX_CHARS`, ellipsis-terminated.

    The notes writers prefix their annotations with a space, so the quote
    is stripped before measuring; the content itself stays verbatim.
    """
    text = text.strip()
    if len(text) <= EVIDENCE_MAX_CHARS:
        return text
    return text[:EVIDENCE_MAX_CHARS] + "…"
