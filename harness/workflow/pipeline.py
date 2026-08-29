"""The task pipeline: spec -> feasibility -> slicing -> slices -> holistic."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..core import prompts
from ..core.config import Config
from ..core.enums import CheckpointStage, ReviewKind, Stage, Verdict
from external.git_cli import GateNotApplicable, is_under_queue
from ..core.gitops import ensure_branch
from ..core.providers import Task
from ..core.session import SessionResult, SessionRunner
from .params import StageContext
from .spec_assessment import SpecAssessment, assess_spec
from .task_lifecycle import TaskLifecycle

# The four stages that have a `stage_*` function to run, in pipeline order
# (F2.1). Deliberately not derived from CHECKPOINT_ORDER: `MERGE` is a
# checkpoint marker with no stage function of its own — `stage_holistic` writes
# it and honours it (F8).
STAGE_SEQUENCE: tuple[CheckpointStage, ...] = (
    CheckpointStage.SPEC,
    CheckpointStage.FEASIBILITY,
    CheckpointStage.SLICING,
    CheckpointStage.SLICES,
)

_STAGE_FUNCTIONS = {
    CheckpointStage.SPEC: "stage_spec",
    CheckpointStage.FEASIBILITY: "stage_feasibility",
    CheckpointStage.SLICING: "stage_slicing",
    CheckpointStage.SLICES: "stage_slices",
}


class AllAttemptsCrashed(Exception):
    """Every crash retry of one stage session died (T57).

    `_run` raises this instead of handing back the last dead result, so a
    verdict read out of a process that never finished can never be routed on.
    `Pipeline.process` catches it once — no stage catches it — and parks with
    `str(exc)`, which names the task, the stage and the number of attempts.
    """

    def __init__(self, task_id: str, stage: Stage | str, attempts: int):
        self.task_id = task_id
        self.stage = stage
        self.attempts = attempts
        super().__init__(f"all {attempts} attempts crashed at stage "
                         f"{_stage_value(stage)} (task {task_id})")


class OverContextBudget(Exception):
    """One session was stopped for crossing the context cap (T74).

    The trip itself happens in the stream (`external/pi_cli.py`) and is lifted
    onto `SessionResult.over_context_budget` by `SessionRunner`; this exception
    is the *routing* of that trip. `_run` raises it before the crash-retry
    branch, so an over-cap session is never retried — re-running a session that
    already proved too big for the window would only burn the same context
    again — and its partial verdict never reaches a stage.

    It carries everything a handoff needs, because the session that tripped is
    the only place those values exist: the stage, slice id and iteration the
    call site asked for, the measured peak, the cap that stopped it, and the
    path of the partial session output. `Pipeline.process` catches it once and
    parks with `str(exc)`; the structured fields are for the handoff rendering
    that reads them off the caught exception.
    """

    def __init__(self, task_id: str, stage: Stage | str, *,
                 slice_id: str | None, iteration: int,
                 peak_tokens: int, context_limit: int | None,
                 out_file: Path | None):
        self.task_id = task_id
        self.stage = stage
        self.slice_id = slice_id
        self.iteration = iteration
        self.peak_tokens = peak_tokens
        self.context_limit = context_limit
        self.out_file = out_file
        super().__init__(f"over context budget: peak={peak_tokens} "
                         f"limit={context_limit}")


class Pipeline:
    def __init__(self, cfg: Config, runner: SessionRunner, log=print, provider=None):
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.provider = provider
        self.lifecycle = TaskLifecycle(cfg, log)
        self.max_crash_retries = cfg.get("maxCrashRetries", 2)

    def _run(self, model, workdir, prompt, *, task_id, stage: Stage | str, **kw):
        """Run a session, retrying on crash. Artifacts persist on disk, so a
        retry continues from what the model already wrote rather than restarting.

        A session that comes back healthy is returned as it stands. When the
        last allowed attempt crashes there is no verdict left to trust, so
        `AllAttemptsCrashed` is raised (T57) and the task is parked by
        `process`; the crashed result is never returned to a stage.

        An over-cap stop is checked first (T74) and raises `OverContextBudget`
        on the spot: it outranks the crash path — a session we stopped on
        purpose is not a child that died and gets no retry at all — and the
        result is never returned, so a stage cannot route on the partial
        verdict a stopped session left behind.

        Call sites pass a `Stage` member (T30); `SessionRunner` converts it at
        the stats edge, so the value travelling through here stays whatever the
        caller handed over. `slice_id` and `iteration` travel in `kw` and are
        read back out of it for the exception payload, with the same defaults
        `SessionRunner.run` uses.
        """
        for attempt in range(self.max_crash_retries + 1):
            r = self.runner.run(model, workdir, prompt, task_id=task_id,
                                stage=stage, **kw)
            if r.over_context_budget:
                raise OverContextBudget(
                    task_id, stage,
                    slice_id=kw.get("slice_id"),
                    iteration=kw.get("iteration", 1),
                    peak_tokens=r.peak_tokens,
                    context_limit=r.context_limit,
                    out_file=r.out_file)
            if not r.crashed:
                return r
            if attempt < self.max_crash_retries:
                self.log(f"  ⚠ {stage} crashed (rc/timeout); retrying "
                         f"({attempt+1}/{self.max_crash_retries}) — artifacts preserved")
        raise AllAttemptsCrashed(task_id, stage, self.max_crash_retries + 1)

    # ------------------------------------------------------------------
    # top level
    # ------------------------------------------------------------------
    def process(self, task: Task) -> str:
        """Run (or resume) a task through the stage waterfall.

        Resume is inherent (F2.1): if `active/<id>/task.json` already exists,
        the task resumes from its checkpointed prefix instead of re-running
        completed stages. A fresh task (no task.json) is intaken as before.
        """
        task_dir = self.lifecycle.task_dir(task.id)
        if self.lifecycle.task_json_path(task.id).exists():
            state = self.lifecycle.load_state(task.id)
            skipped = [s.value for s in state.checkpointed_stages]
            self.log(f"═══ task {task.id} ═══")
            self.log(f"  resuming from checkpoint — skipping: {', '.join(skipped)}" if skipped
                     else f"  resuming (no checkpoints yet)")
            if not state.workdir:
                # Old-format task.json: migrate once from the same persisted
                # original.md intake used, then never re-derive.
                self.log(f"  workdir not recorded for {task.id}, "
                         f"re-derived from original.md")
                self.lifecycle.record_workdir(task_dir)
                state = self.lifecycle.load_state(task.id)
        else:
            task_dir = self.lifecycle.intake(task)
            state = self.lifecycle.load_state(task.id)
            self.log(f"═══ task {task.id} ═══")
        # Body is now persisted in the task dir; drop the pending/claim staging file
        # so this task cannot be re-claimed while it is in flight or terminal.
        if self.provider is not None and hasattr(self.provider, "release_claim"):
            self.provider.release_claim(task)
        workdir = Path(state.workdir)
        self.log(f"  workdir: {workdir}")
        # F7 guard: `ensure_branch` `git init`s a workdir that has no `.git`, so
        # a task that names no repo would otherwise create a real repository —
        # and collect its session `.out` files — inside the queue tree. This is
        # the only call site that knows both the workdir and `cfg.queue_dir`, so
        # the check lives here and `git_cli` stays a dumb git wrapper.
        if is_under_queue(workdir, self.cfg.queue_dir):
            self.lifecycle.park(
                task.id,
                f"refusing to init a repo in the queue: workdir={workdir} is "
                f"under queue={self.cfg.queue_dir}; record the real repo path "
                f"in the task body")
            return "parked"
        if not workdir.is_dir():
            # Same refusal, different reason: a workdir that does not exist is
            # not a repo either, and `ensure_branch` would `git init` into a
            # path created by nothing but the guard's absence. Never mkdir.
            self.lifecycle.park(
                task.id,
                f"refusing to init a repo outside a real directory: "
                f"workdir={workdir} does not exist; record the real repo path "
                f"in the task body")
            return "parked"
        try:
            ensure_branch(workdir, task.id, self.cfg.trunk_branch)
        except Exception as e:
            self.lifecycle.park(task.id, f"git setup failed: {e}")
            return "parked"

        ctx = StageContext(task.id, task_dir, workdir)
        try:
            for stage in STAGE_SEQUENCE:
                if stage in state.checkpointed_stages:
                    self.log(f"  ⏭ skipping {stage.value} (checkpointed)")
                    continue
                self.lifecycle.set_stage(task.id, stage)
                self.log(f"  ▶ {stage.value}")
                if not getattr(self, _STAGE_FUNCTIONS[stage])(ctx):
                    return self._stage_failed(task.id, stage, task_dir)
                self.lifecycle.checkpoint(task.id, stage)
            return self.stage_holistic(ctx)
        except OverContextBudget as e:
            # The one catch site (T74). Crossing the cap is neither a content
            # verdict nor a process death, so it parks whatever stage it
            # happened on — and it is caught here rather than in a stage for
            # the same reason as T57: no stage may route on the partial output
            # of a session that was stopped mid-work.
            self.lifecycle.park(task.id, str(e))
            return "parked"
        except AllAttemptsCrashed as e:
            # The one catch site (T57). A stage that lost every attempt to a
            # dead process is a process failure, not a content verdict, so it
            # parks whatever stage it happened on — including the holistic one,
            # where a merge must not be attempted on unreviewed work.
            self.lifecycle.park(task.id, str(e))
            return "parked"

    def _stage_failed(self, task_id: str, stage: CheckpointStage, task_dir) -> str:
        """Per-stage terminal return contract (F2.1): feasibility failure is
        `failed` only when the dir has already been moved out of active/ by
        `fail()`; every other stage failure parks."""
        if stage == CheckpointStage.FEASIBILITY:
            return "failed" if not task_dir.exists() else "parked"
        return "parked"

    # ------------------------------------------------------------------
    # stage 1: specification
    # ------------------------------------------------------------------
    def stage_spec(self, ctx: StageContext) -> bool:
        """Author the spec, then have both assessors accept it (T43).

        Each assessor must return a healthy `PASS` for the stage to pass. A
        `KICKBACK` from either one re-runs the author under the shared kickback
        maximum; anything else — including a session that did not finish —
        parks with the assessor name and verdict in the reason. The protocol
        itself is `workflow.spec_assessment.assess_spec`.
        """
        kickbacks = 0
        while True:
            r = self._run(
                self.cfg.model, ctx.workdir, prompts.spec_author(ctx.task_dir),
                task_id=ctx.task_id, stage=Stage.SPEC_AUTHOR)
            self.log(f"  spec author verdict: {r.verdict}")
            if r.verdict is not Verdict.DONE:
                r = self._run(
                    self.cfg.model, ctx.workdir, prompts.spec_author(ctx.task_dir),
                    task_id=ctx.task_id, stage=Stage.SPEC_AUTHOR, notes="retry")
                if r.verdict is not Verdict.DONE:
                    self.lifecycle.park(ctx.task_id, "spec author failed twice")
                    return False

            r = self._run(
                self.cfg.assessor, ctx.workdir, prompts.spec_assess(ctx.task_dir, "ornith"),
                task_id=ctx.task_id, stage=Stage.SPEC_ASSESS_ORNITH)
            self.log(f"  ornith assessment verdict: {r.verdict}")
            decision = assess_spec("ornith", r)
            if decision.outcome is SpecAssessment.PARKED:
                self.lifecycle.park(ctx.task_id, decision.reason)
                return False
            if decision.outcome is SpecAssessment.KICKBACK:
                kickbacks = self._spec_kickback(ctx, "ornith", r, kickbacks)
                if kickbacks is None:
                    return False
                continue

            r = self._run(
                self.cfg.model, ctx.workdir, prompts.spec_assess(ctx.task_dir, "tw"),
                task_id=ctx.task_id, stage=Stage.SPEC_ASSESS_TW)
            self.log(f"  TW requirement-check verdict: {r.verdict}")
            decision = assess_spec("tw", r)
            if decision.outcome is SpecAssessment.PARKED:
                self.lifecycle.park(ctx.task_id, decision.reason)
                return False
            if decision.outcome is SpecAssessment.KICKBACK:
                kickbacks = self._spec_kickback(ctx, "tw", r, kickbacks)
                if kickbacks is None:
                    return False
                continue

            self.log("  spec approved")
            return True

    def _spec_kickback(self, ctx: StageContext, assessor: str, r: SessionResult,
                       kickbacks: int) -> int | None:
        """Count one spec kickback and file its report.

        Returns the new count, or None when the maximum is exceeded — the task
        is parked and the caller must stop. The counter is shared by both
        assessors; the artifact name says which one kicked back.
        """
        kickbacks += 1
        if kickbacks > self.cfg.max_spec_kickbacks:
            self.lifecycle.park(ctx.task_id, f"spec kickback loop exceeded ({self.cfg.max_spec_kickbacks})")
            return None
        shutil.copy(r.out_file, ctx.task_dir / "artifacts" / f"kickback_{assessor}_{kickbacks}.md")
        self.log(f"  kickback to spec author (#{kickbacks})")
        return kickbacks

    # ------------------------------------------------------------------
    # stage 2: feasibility
    # ------------------------------------------------------------------
    def stage_feasibility(self, ctx: StageContext) -> bool:
        r = self._run(
            self.cfg.implementer, ctx.workdir, prompts.feasibility(ctx.task_dir),
            task_id=ctx.task_id, stage=Stage.FEASIBILITY)
        self.log(f"  feasibility verdict: {r.verdict}")
        if r.verdict is Verdict.PASS:
            return True
        if r.verdict is Verdict.KICKOUT:
            self.lifecycle.fail(ctx.task_id, "Task rejected at feasibility: " + _summary(r.output))
            return False
        if r.verdict is Verdict.KICKBACK:
            shutil.copy(r.out_file, ctx.task_dir / "artifacts" / "feasibility_kickback.md")
            self.log("  feasibility kickback -> back to spec stage")
            if not self.stage_spec(ctx):
                return False
            r = self._run(
                self.cfg.implementer, ctx.workdir, prompts.feasibility(ctx.task_dir),
                task_id=ctx.task_id, stage=Stage.FEASIBILITY, notes="recheck")
            if r.verdict is Verdict.PASS:
                return True
            self.lifecycle.park(ctx.task_id, "feasibility still failing after spec revision")
            return False
        self.lifecycle.park(ctx.task_id, f"feasibility verdict unclear: {r.verdict}")
        return False

    # ------------------------------------------------------------------
    # stage 3: slicing
    # ------------------------------------------------------------------
    def stage_slicing(self, ctx: StageContext) -> bool:
        r = self._run(
            self.cfg.implementer, ctx.workdir, prompts.slice(ctx.task_dir),
            task_id=ctx.task_id, stage=Stage.SLICING)
        self.log(f"  slicing verdict: {r.verdict}")
        if r.verdict is not Verdict.DONE:
            self.lifecycle.park(ctx.task_id, f"slicing failed (verdict={r.verdict})")
            return False

        fast = self.cfg.fast_pool[0] if self.cfg.fast_pool else self.cfg.implementer
        for check in range(1, self.cfg.max_slice_check_loops + 1):
            r = self._run(
                fast, ctx.workdir, prompts.slice_check(ctx.task_dir),
                task_id=ctx.task_id, stage=Stage.SLICE_CHECK, iteration=check)
            self.log(f"  slice check #{check} verdict: {r.verdict}")
            if r.verdict is Verdict.PASS:
                return True
            if r.verdict is Verdict.RESLICED:
                continue
        self.lifecycle.park(ctx.task_id, "slice fit check loop exceeded")
        return False

    # ------------------------------------------------------------------
    # stage 4: per-slice execution
    # ------------------------------------------------------------------
    def stage_slices(self, ctx: StageContext) -> bool:
        state = self.lifecycle.load_state(ctx.task_id)
        slices = _parse_slices(ctx.task_dir / "artifacts" / "slices.md")
        if not slices:
            self.lifecycle.park(ctx.task_id, "no slices parsed from slices.md")
            return False
        self.log(f"  slices: {' '.join(slices)}")

        for sid in slices:
            # F8: a slice that already passed its last required review is work
            # committed on the task branch — never re-run it on a resume. The
            # stage-level `CheckpointStage.SLICES` marker still answers the
            # other question ("is the whole loop done") and is written by the
            # caller, not here.
            if sid in state.checkpointed_slices:
                self.log(f"  ⏭ skipping slice {sid} (checkpointed)")
                continue
            self.log(f"  ── slice {sid} ──")
            if not self._implement(ctx, sid):
                return False
            if not self._review_loop(ctx, sid, ReviewKind.TECH, Stage.TECH_REVIEW):
                return False
            if not self._review_loop(ctx, sid, ReviewKind.FUNC, Stage.FUNC_REVIEW):
                return False
            self._checkpoint_slice(ctx.task_id, sid)
            self.log(f"    slice {sid} passed all reviews")
        return True

    def _checkpoint_slice(self, task_id: str, sid: str) -> None:
        """Record one finished slice. A lost write only costs a re-run of that
        slice on resume, so it is logged and swallowed rather than parking the
        task whose reviews all passed."""
        try:
            self.lifecycle.checkpoint_slices(task_id, [sid])
        except Exception as e:
            self.log(f"  ⚠ could not checkpoint slice {sid}: {e}")

    def _implement(self, ctx: StageContext, sid: str) -> bool:
        for it in range(1, self.cfg.max_slice_implement + 1):
            r = self._run(
                self.cfg.implementer, ctx.workdir,
                prompts.implement_slice(ctx.task_dir, sid, it, self.cfg.max_slice_implement),
                task_id=ctx.task_id, stage=Stage.SLICE_IMPLEMENT, slice_id=sid, iteration=it)
            self.log(f"    implement iter {it} verdict: {r.verdict}")
            if r.verdict is Verdict.DONE:
                return True
            note = ctx.task_dir / "artifacts" / "progress" / f"slice-{sid}.md"
            if r.verdict is Verdict.PROGRESS and not note.exists():
                shutil.copy(r.out_file, note)
        self.lifecycle.park(ctx.task_id, f"slice {sid} not delivered in {self.cfg.max_slice_implement} implementation iterations")
        return False

    def _review_loop(self, ctx: StageContext, sid: str, kind: ReviewKind,
                     stage: Stage) -> bool:
        max_iter = (self.cfg.max_slice_tech_review if kind is ReviewKind.TECH
                    else self.cfg.max_slice_func_review)
        model = self.cfg.implementer if kind is ReviewKind.TECH else self.cfg.model
        prompt_fn = (prompts.tech_review if kind is ReviewKind.TECH
                     else prompts.func_review)

        for it in range(1, max_iter + 1):
            r = self._run(
                model, ctx.workdir, prompt_fn(ctx.task_dir, sid),
                task_id=ctx.task_id, stage=stage, slice_id=sid, iteration=it)
            self.log(f"    {kind} review iter {it} verdict: {r.verdict}")
            if r.verdict is Verdict.PASS:
                return True
            if it < max_iter:
                # Review feedback and the implementation progress note are two
                # artifacts with two readers (the fix session vs. the next
                # implement session), so they get two paths. Sharing one file let
                # a review report be read back as a progress note and made
                # `_implement`'s "keep the first note" guard stick (T56).
                feedback = (ctx.task_dir / "artifacts" / "progress"
                            / f"slice-{sid}-review.md")
                shutil.copy(r.out_file, feedback)
                # A fix session is a code edit, so it runs on the implementer
                # regardless of which review asked for it (T55). `model` above
                # follows the review type; the fix model follows the work type.
                self._run(
                    self.cfg.implementer, ctx.workdir,
                    prompts.fix_slice(ctx.task_dir, sid, feedback, kind),
                    task_id=ctx.task_id, stage=Stage.SLICE_FIX, slice_id=sid, iteration=it,
                    notes=f"fix after {kind} review")
        self.lifecycle.park(ctx.task_id, f"slice {sid} failed {kind} review after {max_iter} iterations")
        return False

    # ------------------------------------------------------------------
    # stage 5: holistic review + merge
    # ------------------------------------------------------------------
    def stage_holistic(self, ctx: StageContext) -> str:
        if self._is_merged(ctx.task_id):
            # F8: the merge already landed, only the completion move was lost.
            # Re-merging cannot work (the branch content is on trunk) and a
            # second holistic session would cost a model run for nothing.
            self.log("  already merged, completing")
            self.lifecycle.complete(
                ctx.task_id,
                f"Feature complete and merged to {self.cfg.trunk_branch} "
                f"(merge checkpointed by an earlier run).")
            self._cleanup_branch(ctx.workdir, ctx.task_id)
            return "done"

        r = self._run(
            self.cfg.model, ctx.workdir, prompts.holistic_review(ctx.task_dir),
            task_id=ctx.task_id, stage=Stage.HOLISTIC)
        self.log(f"  holistic review verdict: {r.verdict}")
        if r.verdict is Verdict.PASS:
            try:
                original = ctx.task_dir / "original.md"
                title = (original.read_text().strip().splitlines()[0][:70]
                         if original.exists() else ctx.task_id)
                from ..core.gitops import merge_to_trunk
                merge_to_trunk(ctx.workdir, ctx.task_id, self.cfg.trunk_branch, title)
            except GateNotApplicable as e:
                # The harness gate cannot judge this repo. Nothing was mutated
                # (the refusal precedes every git write), the branch is still
                # there for a human (T27), and re-running would refuse again:
                # park with the reason verbatim, never retry.
                self.lifecycle.park(ctx.task_id, str(e))
                return "parked"
            except Exception as e:
                self.lifecycle.park(ctx.task_id, f"merge failed: {e}")
                return "parked"
            self._checkpoint_merge(ctx.task_id)
            self.lifecycle.complete(ctx.task_id, "Feature complete and merged to "
                         f"{self.cfg.trunk_branch}. " + _summary(r.output))
            self._cleanup_branch(ctx.workdir, ctx.task_id)
            return "done"
        self.lifecycle.park(ctx.task_id, "holistic review failed: " + _summary(r.output))
        return "parked"

    # ------------------------------------------------------------------
    # merge checkpoint (F8)
    # ------------------------------------------------------------------
    def _is_merged(self, task_id: str) -> bool:
        """True when a previous run recorded `CheckpointStage.MERGE`."""
        if not self.lifecycle.task_json_path(task_id).exists():
            return False
        state = self.lifecycle.load_state(task_id)
        return CheckpointStage.MERGE in state.checkpointed_stages

    def _checkpoint_merge(self, task_id: str) -> None:
        """Record the merge before completing. A lost checkpoint write is the
        lesser evil — leaving an already-merged task stuck in active/ is not."""
        try:
            self.lifecycle.checkpoint(task_id, CheckpointStage.MERGE)
        except Exception as e:
            self.log(f"  ⚠ could not checkpoint the merge: {e} — completing anyway")

    # ------------------------------------------------------------------
    # post-complete branch cleanup (F8)
    # ------------------------------------------------------------------
    def _cleanup_branch(self, workdir: Path, task_id: str) -> None:
        """Drop `pi/<task_id>` now that the task is done (F8).

        Called only after `complete()` returned, never before: until the task
        dir has actually landed in done/ the branch is the only copy of the
        work's provenance and a resumed run still needs it. A failed cleanup is
        cosmetic — the work is on trunk and the task is complete — so it is
        logged and swallowed; it must never park or fail a done task.
        """
        from ..core.gitops import cleanup_branch
        try:
            cleanup_branch(workdir, task_id, self.cfg.trunk_branch)
        except Exception as e:
            self.log(f"  ⚠ task {task_id} is complete but branch "
                     f"pi/{task_id} was not deleted: {e}")


# ---------------------------------------------------------------------------

def _stage_value(stage: Stage | str) -> str:
    """A `Stage` member's wire name; a stray string passes through unchanged
    (the same tolerant conversion `SessionRunner.run` makes at the stats edge)."""
    return stage.value if isinstance(stage, Stage) else str(stage)


def _parse_slices(path: Path) -> list[str]:
    if not path.exists():
        return []
    ids = []
    for m in re.finditer(r"^###[ \t]+Slice[ \t]+([0-9]+(?:\.[0-9]+)*)",
                         path.read_text(), re.MULTILINE):
        ids.append(m.group(1))
    return sorted(set(ids), key=lambda s: [int(x) for x in s.split(".")])


def _summary(output: str, n: int = 6) -> str:
    m = re.search(r"## Summary\n(.+?)(?:\nVERDICT:|\Z)", output, re.DOTALL)
    if not m:
        return output.strip()[-400:]
    return " ".join(m.group(1).split())[:400]