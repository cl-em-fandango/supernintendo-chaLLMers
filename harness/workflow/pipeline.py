"""The task pipeline: spec -> feasibility -> slicing -> slices -> holistic."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..core import prompts
from ..core.config import Config
from ..core.enums import CheckpointStage
from ..core.gitops import ensure_branch
from ..core.providers import Task
from ..core.session import SessionRunner
from .params import StageContext
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


class Pipeline:
    def __init__(self, cfg: Config, runner: SessionRunner, log=print, provider=None):
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.provider = provider
        self.lifecycle = TaskLifecycle(cfg, log)
        self.max_crash_retries = cfg.get("maxCrashRetries", 2)

    def _run(self, model, workdir, prompt, *, task_id, stage, **kw):
        """Run a session, retrying on crash. Artifacts persist on disk, so a
        retry continues from what the model already wrote rather than restarting."""
        for attempt in range(self.max_crash_retries + 1):
            r = self.runner.run(model, workdir, prompt, task_id=task_id,
                                stage=stage, **kw)
            if not r.crashed:
                return r
            if attempt < self.max_crash_retries:
                self.log(f"  ⚠ {stage} crashed (rc/timeout); retrying "
                         f"({attempt+1}/{self.max_crash_retries}) — artifacts preserved")
        return r

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
        else:
            task_dir = self.lifecycle.intake(task)
            state = self.lifecycle.load_state(task.id)
            self.log(f"═══ task {task.id} ═══")
        # Body is now persisted in the task dir; drop the pending/claim staging file
        # so this task cannot be re-claimed while it is in flight or terminal.
        if self.provider is not None and hasattr(self.provider, "release_claim"):
            self.provider.release_claim(task)
        workdir = self.lifecycle.resolve_workdir(task_dir)
        self.log(f"  workdir: {workdir}")
        try:
            ensure_branch(workdir, task.id, self.cfg.trunk_branch)
        except Exception as e:
            self.lifecycle.park(task.id, f"git setup failed: {e}")
            return "parked"

        ctx = StageContext(task.id, task_dir, workdir)
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
        kickbacks = 0
        while True:
            r = self._run(
                self.cfg.model, ctx.workdir, prompts.spec_author(ctx.task_dir),
                task_id=ctx.task_id, stage="spec_author")
            self.log(f"  spec author verdict: {r.verdict}")
            if r.verdict != "done":
                r = self._run(
                    self.cfg.model, ctx.workdir, prompts.spec_author(ctx.task_dir),
                    task_id=ctx.task_id, stage="spec_author", notes="retry")
                if r.verdict != "done":
                    self.lifecycle.park(ctx.task_id, "spec author failed twice")
                    return False

            r = self._run(
                self.cfg.assessor, ctx.workdir, prompts.spec_assess(ctx.task_dir, "ornith"),
                task_id=ctx.task_id, stage="spec_assess_ornith")
            self.log(f"  ornith assessment verdict: {r.verdict}")
            if r.verdict == "kickback":
                kickbacks += 1
                if kickbacks > self.cfg.max_spec_kickbacks:
                    self.lifecycle.park(ctx.task_id, f"spec kickback loop exceeded ({self.cfg.max_spec_kickbacks})")
                    return False
                shutil.copy(r.out_file, ctx.task_dir / "artifacts" / f"kickback_ornith_{kickbacks}.md")
                self.log(f"  kickback to spec author (#{kickbacks})")
                continue

            r = self._run(
                self.cfg.model, ctx.workdir, prompts.spec_assess(ctx.task_dir, "tw"),
                task_id=ctx.task_id, stage="spec_assess_tw")
            self.log(f"  TW requirement-check verdict: {r.verdict}")
            if r.verdict == "kickback":
                kickbacks += 1
                if kickbacks > self.cfg.max_spec_kickbacks:
                    self.lifecycle.park(ctx.task_id, f"spec kickback loop exceeded ({self.cfg.max_spec_kickbacks})")
                    return False
                shutil.copy(r.out_file, ctx.task_dir / "artifacts" / f"kickback_tw_{kickbacks}.md")
                self.log(f"  kickback to spec author (#{kickbacks})")
                continue

            self.log("  spec approved")
            return True

    # ------------------------------------------------------------------
    # stage 2: feasibility
    # ------------------------------------------------------------------
    def stage_feasibility(self, ctx: StageContext) -> bool:
        r = self._run(
            self.cfg.implementer, ctx.workdir, prompts.feasibility(ctx.task_dir),
            task_id=ctx.task_id, stage="feasibility")
        self.log(f"  feasibility verdict: {r.verdict}")
        if r.verdict == "pass":
            return True
        if r.verdict == "kickout":
            self.lifecycle.fail(ctx.task_id, "Task rejected at feasibility: " + _summary(r.output))
            return False
        if r.verdict == "kickback":
            shutil.copy(r.out_file, ctx.task_dir / "artifacts" / "feasibility_kickback.md")
            self.log("  feasibility kickback -> back to spec stage")
            if not self.stage_spec(ctx):
                return False
            r = self._run(
                self.cfg.implementer, ctx.workdir, prompts.feasibility(ctx.task_dir),
                task_id=ctx.task_id, stage="feasibility", notes="recheck")
            if r.verdict == "pass":
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
            task_id=ctx.task_id, stage="slicing")
        self.log(f"  slicing verdict: {r.verdict}")
        if r.verdict != "done":
            self.lifecycle.park(ctx.task_id, f"slicing failed (verdict={r.verdict})")
            return False

        fast = self.cfg.fast_pool[0] if self.cfg.fast_pool else self.cfg.implementer
        for check in range(1, self.cfg.max_slice_check_loops + 1):
            r = self._run(
                fast, ctx.workdir, prompts.slice_check(ctx.task_dir),
                task_id=ctx.task_id, stage="slice_check", iteration=check)
            self.log(f"  slice check #{check} verdict: {r.verdict}")
            if r.verdict == "pass":
                return True
            if r.verdict == "resliced":
                continue
        self.lifecycle.park(ctx.task_id, "slice fit check loop exceeded")
        return False

    # ------------------------------------------------------------------
    # stage 4: per-slice execution
    # ------------------------------------------------------------------
    def stage_slices(self, ctx: StageContext) -> bool:
        slices = _parse_slices(ctx.task_dir / "artifacts" / "slices.md")
        if not slices:
            self.lifecycle.park(ctx.task_id, "no slices parsed from slices.md")
            return False
        self.log(f"  slices: {' '.join(slices)}")

        for sid in slices:
            self.log(f"  ── slice {sid} ──")
            if not self._implement(ctx, sid):
                return False
            if not self._review_loop(ctx, sid, "tech"):
                return False
            if not self._review_loop(ctx, sid, "func"):
                return False
            self.log(f"    slice {sid} passed all reviews")
        return True

    def _implement(self, ctx: StageContext, sid: str) -> bool:
        for it in range(1, self.cfg.max_slice_implement + 1):
            r = self._run(
                self.cfg.implementer, ctx.workdir,
                prompts.implement_slice(ctx.task_dir, sid, it, self.cfg.max_slice_implement),
                task_id=ctx.task_id, stage="slice_implement", slice_id=sid, iteration=it)
            self.log(f"    implement iter {it} verdict: {r.verdict}")
            if r.verdict == "done":
                return True
            note = ctx.task_dir / "artifacts" / "progress" / f"slice-{sid}.md"
            if r.verdict == "progress" and not note.exists():
                shutil.copy(r.out_file, note)
        self.lifecycle.park(ctx.task_id, f"slice {sid} not delivered in {self.cfg.max_slice_implement} implementation iterations")
        return False

    def _review_loop(self, ctx: StageContext, sid: str, kind: str) -> bool:
        max_iter = (self.cfg.max_slice_tech_review if kind == "tech"
                    else self.cfg.max_slice_func_review)
        model = self.cfg.implementer if kind == "tech" else self.cfg.model
        prompt_fn = prompts.tech_review if kind == "tech" else prompts.func_review
        stage = f"{kind}_review"

        for it in range(1, max_iter + 1):
            r = self._run(
                model, ctx.workdir, prompt_fn(ctx.task_dir, sid),
                task_id=ctx.task_id, stage=stage, slice_id=sid, iteration=it)
            self.log(f"    {kind} review iter {it} verdict: {r.verdict}")
            if r.verdict == "pass":
                return True
            if it < max_iter:
                feedback = ctx.task_dir / "artifacts" / "progress" / f"slice-{sid}.md"
                shutil.copy(r.out_file, feedback)
                self._run(
                    model, ctx.workdir, prompts.fix_slice(ctx.task_dir, sid, feedback, kind),
                    task_id=ctx.task_id, stage="slice_fix", slice_id=sid, iteration=it,
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
            return "done"

        r = self._run(
            self.cfg.model, ctx.workdir, prompts.holistic_review(ctx.task_dir),
            task_id=ctx.task_id, stage="holistic")
        self.log(f"  holistic review verdict: {r.verdict}")
        if r.verdict == "pass":
            try:
                original = ctx.task_dir / "original.md"
                title = (original.read_text().strip().splitlines()[0][:70]
                         if original.exists() else ctx.task_id)
                from ..core.gitops import merge_to_trunk
                merge_to_trunk(ctx.workdir, ctx.task_id, self.cfg.trunk_branch, title)
            except Exception as e:
                self.lifecycle.park(ctx.task_id, f"merge failed: {e}")
                return "parked"
            self._checkpoint_merge(ctx.task_id)
            self.lifecycle.complete(ctx.task_id, "Feature complete and merged to "
                         f"{self.cfg.trunk_branch}. " + _summary(r.output))
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


# ---------------------------------------------------------------------------

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