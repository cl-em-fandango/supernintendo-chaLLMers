"""The task pipeline: spec -> feasibility -> slicing -> slices -> holistic."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..core import prompts
from ..core.config import Config
from ..core.health import HealthOutcome, wait_for_healthy_server
from ..core.enums import CheckpointStage, ReviewKind, Stage, Verdict
from external.git_cli import GateNotApplicable, is_under_queue
from ..core.gitops import ensure_branch
from ..core.providers import DEMO_META_KEY, Task
from ..core.session import SessionResult, SessionRunner
from ..core.stats import render_task_journey_markdown
from ..core.sync_stage_change_hook import run_stage_change_hook
from ..core.transcripts import (
    list_transcripts,
    match_rows_to_transcripts,
    resolve_task_dir,
)
from .continuation import (ContinuationNote, continuation_prompt, handover_dir,
                           write_note)
from .params import StageContext
from .spec_assessment import SpecAssessment, assess_spec
from .task_lifecycle import Handoff, TaskLifecycle

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
    """A stage crossed the context cap and no handover could rescue it (T74).

    The trip itself happens in the stream (`external/pi_cli.py`) and is lifted
    onto `SessionResult.over_context_budget` by `SessionRunner`. A trip is a
    budget *warning*, not a verdict, so `_run` no longer treats the first one as
    terminal: it writes a handover note and resumes the stage in a clean session
    (`workflow/continuation.py`). This exception is raised only when
    `maxContextContinuations` fresh sessions have all tripped on the same stage
    — the point where the work really is too big for one session and a human or
    a re-slice is needed.

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


class ServerUnhealthy(Exception):
    """The FR-5.1 pre-flight exhausted its backoff before a dispatch.

    Deliberately distinct from `AllAttemptsCrashed`: no session was ever
    spawned, so no crash-retry attempt was spent. `Pipeline.process`
    catches it once and parks with the reason — the checkpointed prefix
    stays intact, so a later `--continue` resumes once the server is back.
    """

    def __init__(self, task_id: str, stage: Stage | str, attempts: int,
                 waited_s: float):
        self.task_id = task_id
        self.stage = stage
        self.attempts = attempts
        self.waited_s = waited_s
        super().__init__(f"LLM server unhealthy: {attempts} health probes "
                         f"over {waited_s:.1f}s before "
                         f"{_stage_value(stage)} (task {task_id})")


class StoodDown(Exception):
    """An interrupt request was active at the boundary before a dispatch.

    `Pipeline._run_attempts` raises this instead of spawning the next `pi`
    session (spec FR-6.1: the safe boundary is immediately before a spawn).
    Deliberately distinct from `AllAttemptsCrashed`/`ServerUnhealthy`: work
    was not lost and the server is fine, so `Pipeline.process` catches it
    once and returns without parking or crash-retrying (FR-6.2/FR-6.4) —
    the task stays in `active/` with its checkpoints and claim (FR-6.5).
    The request itself is already acknowledged (`requested -> paused`) by
    the `stand_down_check` that answered True.
    """

    def __init__(self, task_id: str, stage: Stage | str):
        self.task_id = task_id
        self.stage = stage
        super().__init__(f"stood down at session boundary before "
                         f"{_stage_value(stage)} (task {task_id})")


class Pipeline:
    def __init__(self, cfg: Config, runner: SessionRunner, log=print, provider=None,
                 health_wait=wait_for_healthy_server, stand_down_check=None,
                 handoff_sync=None, sync_engine=None, placeholder_hook=None,
                 demo_app_generator=None, final_deploy_hook=None):
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.provider = provider
        # GitHub handoff hook (spec FR-2.5, FR-3): posts the handoff
        # comment and runs the in-flight sync pass. Built by the
        # composition root only when sync is enabled; shared with the
        # lifecycle so every write site posts through one dedup map.
        self.handoff_sync = handoff_sync
        # The GitHub sync dispatcher (spec FR-3), built once by the
        # composition root and shared with the lifecycle hook sites; None
        # when GitHub is unconfigured, which makes every hook a no-op
        # (FR-0.1, NFR-2).
        self.sync_engine = sync_engine
        self.lifecycle = TaskLifecycle(
            cfg, log, handoff_sync=handoff_sync,
            stage_change_sync=(
                (lambda task_id: run_stage_change_hook(sync_engine, task_id,
                                                       log=log))
                if sync_engine is not None else None))
        self.max_crash_retries = cfg.get("maxCrashRetries", 2)
        # How many *clean* sessions one stage may be handed to after a cap trip.
        # A trip is a warning, so the first response is a handover, not a park;
        # this is the bound on how often we hand over before calling it.
        self.max_context_continuations = cfg.get("maxContextContinuations", 3)
        # FR-5.1 pre-flight, injectable so tests observe the gate without
        # a live server. The default reads the policy from config; with no
        # endpoint configured it is a no-op (NFR-2).
        self.health_wait = health_wait
        # FR-6.1 boundary gate, injectable like `health_wait`: a callable
        # answering True when an interrupt is active (and already
        # acknowledged). None means the wiring has no interrupt to honor.
        self.stand_down_check = stand_down_check
        # Demo spec FR-2/FR-6.2: the pre-spec placeholder deploy hook,
        # injected by the composition root only when `demo.enabled` and
        # GitHub sync are both configured; None makes it a no-op. Called
        # as `placeholder_hook(task, workdir)` before `stage_spec`, and it
        # must never block the spec work (FR-2.3).
        self.placeholder_hook = placeholder_hook
        # Demo spec FR-3: the app generation driver, injected by the
        # composition root only when `demo.enabled`; None leaves the
        # implement stage on its normal prompt with no generation run.
        # Called as `demo_app_generator(ctx)` once per demo task, before
        # that task's first implement session. The supervisor reuses one
        # Pipeline across many tasks, so the "already generated" marker
        # is keyed on the task id, not a run-wide boolean.
        self.demo_app_generator = demo_app_generator
        self._demo_app_generated_for: str | None = None
        # Demo spec FR-6.2 (final half): the post-merge deployment hook,
        # injected by the composition root only when `demo.enabled` and
        # GitHub sync are configured; None makes it a no-op. Called as
        # `final_deploy_hook(ctx)` inside `stage_holistic` after
        # `merge_to_trunk` succeeded and before the completion move, so
        # a failed Pages deployment routes the task to `failed/` (FR-8.1)
        # instead of completing it. The hook returns "" on success and a
        # failure reason otherwise; it comments on the issue itself.
        self.final_deploy_hook = final_deploy_hook

    def _run(self, model, workdir, prompt, *, task_id, stage: Stage | str, **kw):
        """Run a session, retrying a crash and handing over on a cap trip.

        Two failures with two responses:

        - a *dead child* is retried on the same prompt (T57). Artifacts persist
          on disk, so a retry continues from what the model already wrote;
        - a session *stopped for crossing the context cap* is not retried on the
          same prompt — that would burn the same window again. It is handed to a
          fresh session under a handover note (T74's continuation), which is a
          genuinely different run with a different context, not a retry.

        A session that comes back healthy is returned as it stands. When the
        last allowed crash attempt dies there is no verdict left to trust, so
        `AllAttemptsCrashed` is raised (T57); when the last allowed continuation
        still trips, `OverContextBudget` is raised (T74). Neither result is ever
        returned to a stage, so no stage can route on the partial verdict of a
        session that never finished.

        Call sites pass a `Stage` member (T30); `SessionRunner` converts it at
        the stats edge, so the value travelling through here stays whatever the
        caller handed over. `slice_id` and `iteration` travel in `kw` and are
        read back out of it for the note and the exception payload, with the
        same defaults `SessionRunner.run` uses.
        """
        session_prompt = prompt
        handed_over = 0
        while True:
            r = self._run_attempts(model, workdir, session_prompt,
                                   task_id=task_id, stage=stage, **kw)
            if not r.over_context_budget:
                return r
            if handed_over >= self.max_context_continuations:
                raise OverContextBudget(
                    task_id, stage,
                    slice_id=kw.get("slice_id"),
                    iteration=kw.get("iteration", 1),
                    peak_tokens=r.peak_tokens,
                    context_limit=r.context_limit,
                    out_file=r.out_file)
            handed_over += 1
            note = write_note(
                handover_dir(self._task_dir(task_id), r.out_file),
                ContinuationNote(
                    stage=stage,
                    attempt=handed_over,
                    peak_tokens=r.peak_tokens,
                    context_limit=r.context_limit,
                    slice_id=kw.get("slice_id"),
                    iteration=kw.get("iteration", 1),
                    output_path=r.out_file,
                    task_id=task_id or ""),
                r.output,
                handoff_sync=self.handoff_sync)
            self.log(f"  ⚠ {_stage_value(stage)} crossed the context cap "
                     f"(peak={r.peak_tokens}, limit={r.context_limit}); "
                     f"handing over to a clean session "
                     f"({handed_over}/{self.max_context_continuations}) — "
                     f"{note.note_path}")
            session_prompt = continuation_prompt(prompt, note)

    def _run_attempts(self, model, workdir, prompt, *, task_id,
                      stage: Stage | str, **kw) -> SessionResult:
        """One prompt, crash-retried. Returns the healthy result or the trip.

        The over-cap check comes first (T74): it outranks the crash path — a
        session we stopped on purpose is not a child that died — so a tripped
        session costs exactly one runner call and is handed back for the
        handover rather than retried on the same context.
        """
        for attempt in range(self.max_crash_retries + 1):
            self._stand_down_preflight(task_id, stage)
            self._health_preflight(task_id, stage)
            r = self.runner.run(model, workdir, prompt, task_id=task_id,
                                stage=stage, **kw)
            if r.over_context_budget or not r.crashed:
                return r
            if attempt < self.max_crash_retries:
                self.log(f"  ⚠ {stage} crashed (rc/timeout); retrying "
                         f"({attempt+1}/{self.max_crash_retries}) — artifacts preserved")
        raise AllAttemptsCrashed(task_id, stage, self.max_crash_retries + 1)

    def _task_dir(self, task_id) -> Path | None:
        """The task's `active/` dir, or None for a session with no task."""
        return self.lifecycle.task_dir(task_id) if task_id else None

    def _stand_down_preflight(self, task_id: str, stage: Stage | str) -> None:
        """FR-6.1: never dispatch a session past an active interrupt.

        Runs before every `runner.run` call — including before a crash
        retry and before each context-cap continuation — so a task whose
        waterfall started before the request spawns no further session.
        Runs ahead of the health pre-flight: a harness that is standing
        down must not wait on the server either. With no check wired this
        is a no-op.
        """
        if self.stand_down_check is not None and self.stand_down_check():
            raise StoodDown(task_id, stage)

    def _health_preflight(self, task_id: str, stage: Stage | str) -> None:
        """FR-5.1: never dispatch a session to a known-unhealthy server.

        Runs before every `runner.run` call — including before a crash
        retry, where the server may have died between attempts — so an
        outage is waited out instead of burning crash-retry attempts.
        With no endpoint configured the gate is DISABLED and this is a
        no-op: no probe, no sleep, no log line (NFR-2). Exhausted backoff
        raises `ServerUnhealthy`, a distinct outcome that never reaches
        the crash path.
        """
        gate = self.health_wait(self.cfg.health_policy(), log=self.log)
        if gate.outcome is HealthOutcome.UNHEALTHY:
            raise ServerUnhealthy(task_id, stage, gate.attempts,
                                  gate.total_wait_s)

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
        resumed = self.lifecycle.task_json_path(task.id).exists()
        # Front and centre: every run announces which workflow it picked
        # (demo vs standard) on the banner line, so a demo request silently
        # falling back to the standard waterfall is visible at a glance.
        is_demo = bool(task.meta.get(DEMO_META_KEY))
        workflow_name = "demo (snes-demo)" if is_demo else "standard"
        if resumed:
            state = self.lifecycle.load_state(task.id)
            skipped = [s.value for s in state.checkpointed_stages]
            self.log(f"═══ task {task.id} ═══ workflow = {workflow_name}")
            self.log(f"  resuming from checkpoint — skipping: {', '.join(skipped)}" if skipped
                     else f"  resuming (no checkpoints yet)")
            target_codebase = getattr(self.cfg, "target_codebase_dir", None) or getattr(self.cfg, "repo_dir", None)
            if not state.workdir:
                # Old-format task.json: migrate once, then never re-derive.
                self.log(f"  workdir not recorded for {task.id}, "
                         f"resolved from config")
                self.lifecycle.record_workdir(task_dir)
                state = self.lifecycle.load_state(task.id)
            elif target_codebase is not None and state.workdir != str(target_codebase):
                state.workdir = str(target_codebase)
                self.lifecycle.save_state(state)
            elif not (task_dir / "original.md").exists():
                # EC13: original.md missing after a partial crash; re-resolve
                # the workdir so it falls back to the task dir.
                self.lifecycle.record_workdir(task_dir)
                state = self.lifecycle.load_state(task.id)
        else:
            task_dir = self.lifecycle.intake(task)
            state = self.lifecycle.load_state(task.id)
            self.log(f"═══ task {task.id} ═══ workflow = {workflow_name}")
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
                f"under queue={self.cfg.queue_dir}; configure targetCodebaseDir in "
                f"config.json or pass --repo on the CLI")
            return "parked"
        if not workdir.is_dir():
            # Same refusal, different reason: a workdir that does not exist is
            # not a repo either, and `ensure_branch` would `git init` into a
            # path created by nothing but the guard's absence. Never mkdir.
            self.lifecycle.park(
                task.id,
                f"refusing to init a repo outside a real directory: "
                f"workdir={workdir} does not exist; configure targetCodebaseDir in "
                f"config.json or pass --repo on the CLI")
            return "parked"
        try:
            ensure_branch(workdir, task.id, self.cfg.trunk_branch)
        except Exception as e:
            self.lifecycle.park(task.id, f"git setup failed: {e}")
            return "parked"
        if resumed and not self._clean_worktree_on_resume(task.id, workdir):
            return "parked"
        self._deploy_demo_placeholder(task, state, workdir)

        ctx = StageContext(task.id, task_dir, workdir, demo=is_demo)
        outcome = "parked"
        try:
            for stage in STAGE_SEQUENCE:
                if stage in state.checkpointed_stages:
                    self.log(f"  ⏭ skipping {stage.value} (checkpointed)")
                    continue
                self.lifecycle.set_stage(task.id, stage)
                self.log(f"  ▶ {stage.value}")
                if not getattr(self, _STAGE_FUNCTIONS[stage])(ctx):
                    outcome = self._stage_failed(task.id, stage, task_dir)
                    return outcome
                self.lifecycle.checkpoint(task.id, stage)
            outcome = self.stage_holistic(ctx)
            return outcome
        except OverContextBudget as e:
            # The one catch site (T74), reached only after every handover to a
            # clean session also tripped. Crossing the cap is neither a content
            # verdict nor a process death, so it parks whatever stage it
            # happened on — and it is caught here rather than in a stage for
            # the same reason as T57: no stage may route on the partial output
            # of a session that was stopped mid-work.
            #
            # The handoff (T75) carries the fields off the caught exception —
            # the only place they exist — plus the resume position read from
            # `task.json` while the task is still in active/, so the next agent
            # can see how far the run got before it was stopped.
            state = self.lifecycle.load_state(task.id)
            self.lifecycle.park(
                task.id, str(e),
                handoff=Handoff(
                    stage=e.stage,
                    slice_id=e.slice_id,
                    iteration=e.iteration,
                    peak_tokens=e.peak_tokens,
                    context_limit=e.context_limit,
                    output_path=e.out_file,
                    checkpointed_stages=list(state.checkpointed_stages),
                    checkpointed_slices=list(state.checkpointed_slices),
                ))
            outcome = "parked"
            return outcome
        except AllAttemptsCrashed as e:
            # The one catch site (T57). A stage that lost every attempt to a
            # dead process is a process failure, not a content verdict, so it
            # parks whatever stage it happened on — including the holistic one,
            # where a merge must not be attempted on unreviewed work.
            self.lifecycle.park(task.id, str(e))
            outcome = "parked"
            return outcome
        except ServerUnhealthy as e:
            # The one catch site (FR-5.1). The server was unreachable, so no
            # session ever ran and no crash retry was spent; parking keeps the
            # checkpointed prefix intact for a later `--continue` once the
            # server is back. Distinct from the crash path above by design.
            self.lifecycle.park(task.id, str(e))
            outcome = "parked"
            return outcome
        except StoodDown:
            # The one catch site (FR-6.2). An interrupt is neither a content
            # verdict nor a process failure: no parking, no crash-retry, no
            # revert streak. The task keeps its stage/slice checkpoints in
            # active/ and its claim, for `resume` to continue later
            # (FR-6.4/FR-6.5). The check already logged the stand-down and
            # acknowledged the file; the calling loop sees the file at its
            # own next boundary and unwinds.
            outcome = "stood_down"
            return outcome
        finally:
            self._persist_journey_readout(task.id, task_dir)

    def _deploy_demo_placeholder(self, task: Task, state, workdir: Path) -> None:
        """Demo spec FR-2.1/FR-6.2: the placeholder fires before `stage_spec`
        on a claimed demo task, once, and never blocks the pipeline.

        Gated three ways: no hook wired (feature off or GitHub
        unconfigured), a task without the demo flag (FR-6.3: non-demo
        tasks are entirely unaffected), or a resume whose SPEC stage is
        already checkpointed (documented limitation in FR-1.4: a task that
        has passed the pre-spec hook never retroactively deploys a
        placeholder). The hook swallows and comments its own failures;
        this guard is the second line of FR-2.3: even a broken hook must
        not cost the task its spec work.
        """
        if self.placeholder_hook is None:
            return
        if not task.meta.get(DEMO_META_KEY):
            return
        if CheckpointStage.SPEC in state.checkpointed_stages:
            return
        try:
            self.placeholder_hook(task, workdir)
        except Exception as e:  # noqa: BLE001 - FR-2.3: spec work continues
            self.log(f"  ⚠ placeholder hook failed: {e} — continuing into spec")

    def _clean_worktree_on_resume(self, task_id: str, workdir: Path) -> bool:
        """FR-5.3: discard the uncommitted residue of a killed attempt before
        the resumed waterfall starts, so partial edits cannot leak into the
        next slice. Runs only on a resume (a fresh intake has no residue) and
        only after `ensure_branch` put the worktree on the task branch — the
        helper itself refuses trunk/detached HEAD as a second line of defence.

        A clean tree is a no-op. A refused or failed cleanup parks the task:
        proceeding with residue we could not discard is exactly the leak this
        requirement exists to prevent (fail-closed, NFR-1).
        """
        from ..core.gitops import discard_task_residue
        try:
            discarded = discard_task_residue(workdir, task_id,
                                             self.cfg.trunk_branch)
        except Exception as e:
            self.log(f"  WORKTREE-CLEANUP {task_id}: FAILED: {e}")
            self.lifecycle.park(task_id, f"worktree cleanup on resume failed: {e}")
            return False
        if discarded:
            self.log(f"  WORKTREE-CLEANUP {task_id}: discarded "
                     f"{len(discarded)} uncommitted path(s), e.g. {discarded[:5]}")
        else:
            self.log(f"  WORKTREE-CLEANUP {task_id}: clean (no residue)")
        return True

    def _persist_journey_readout(self, task_id: str, task_dir: Path) -> None:
        """Persist the journey readouts (ASCII + Markdown) and log the summary."""
        if not hasattr(self.runner, "store") or self.runner.store is None:
            return
        rows: list[dict] = []
        try:
            from ..core.stats import render_task_journey
            rows = self.runner.store.for_task(task_id)
            if not rows:
                return
            journey_text = render_task_journey(rows, task_id=task_id)
            self.runner.store.write_task_journey(task_id)
            art_dir = task_dir / "artifacts"
            if art_dir.exists():
                # `errors="replace"`: a row carrying text that cannot be
                # encoded (a lone surrogate from a broken child) must not
                # take the whole readout down — the spec's encoding rule is
                # replacement, never an escaped UnicodeEncodeError.
                (art_dir / "journey.txt").write_text(
                    journey_text, encoding="utf-8", errors="replace")
            self.log(f"\n{journey_text}")
        except Exception as e:
            self.log(f"  ⚠ could not persist workflow journey readout: {e}")
        # The browsable Markdown journey gets its own guard: a failure on the
        # legacy ASCII surface above must not cost the operator the transcript
        # index, and a journey failure never changes the pipeline outcome.
        if not rows:
            return
        try:
            self._persist_journey_markdown(task_id, rows)
        except Exception as e:
            self.log(f"  ⚠ could not persist journey.md: {e}")

    def _persist_journey_markdown(self, task_id: str, rows: list[dict]) -> None:
        """Write `artifacts/journey.md` beside the transcripts it links to.

        The caller's `task_dir` is the path from *before* the pass ended, and a
        pass that parked, failed or completed has already moved the task into
        `parked/`, `failed/` or `done/`. The Markdown journey is the browsable
        surface — every link in it is relative to the `artifacts/` directory it
        sits in — so it is written to the task's current location, which is
        where its transcripts are. A task with no queue directory (a direct
        runner use, or a queue that was cleaned mid-pass) simply gets no
        journey.md; the ASCII readout and the stats-dir copy are untouched.
        """
        task_dir = resolve_task_dir(self.cfg.queue_dir, task_id)
        if task_dir is None:
            return
        art_dir = task_dir / "artifacts"
        art_dir.mkdir(parents=True, exist_ok=True)
        transcripts = match_rows_to_transcripts(rows, list_transcripts(task_dir))
        (art_dir / "journey.md").write_text(
            render_task_journey_markdown(rows, task_id=task_id,
                                         transcript_files=transcripts),
            encoding="utf-8", errors="replace")

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
        file_session_output(r, ctx.task_dir / "artifacts" / f"kickback_{assessor}_{kickbacks}.md")
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
            file_session_output(r, ctx.task_dir / "artifacts" / "feasibility_kickback.md")
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
        if ctx.demo:
            self._generate_demo_app(ctx)
        # Demo spec FR-3/FR-6.3: a demo task's implement sessions get the
        # demo prompt variant (the normal prompt plus the app appendix);
        # non-demo tasks keep the untouched prompt.
        prompt_fn = (prompts.implement_slice_demo if ctx.demo
                     else prompts.implement_slice)
        for it in range(1, self.cfg.max_slice_implement + 1):
            r = self._run(
                self.cfg.implementer, ctx.workdir,
                prompt_fn(ctx.task_dir, sid, it, self.cfg.max_slice_implement),
                task_id=ctx.task_id, stage=Stage.SLICE_IMPLEMENT, slice_id=sid, iteration=it)
            self.log(f"    implement iter {it} verdict: {r.verdict}")
            if r.verdict is Verdict.DONE:
                return True
            note = ctx.task_dir / "artifacts" / "progress" / f"slice-{sid}.md"
            if r.verdict is Verdict.PROGRESS and not note.exists():
                file_session_output(r, note)
        self.lifecycle.park(ctx.task_id, f"slice {sid} not delivered in {self.cfg.max_slice_implement} implementation iterations")
        return False

    def _generate_demo_app(self, ctx: StageContext) -> None:
        """Demo spec FR-3: run the app generation driver once per demo
        task, before that task's first implement session.

        The generator (scaffold + content + build, `demo_generate`) is
        wired by the composition root only when `demo.enabled`; without
        it the demo task still gets the demo prompt variant and nothing
        else changes. A raising generator is logged, never fatal — the
        implementer session works on the repo as it stands (failure
        routing and issue comments are the failure-handling slice)."""
        if self.demo_app_generator is None:
            # Visible fallback: a demo task with no generator wired means
            # `demo.enabled` is off (or GitHub is unconfigured) while the
            # task still carries the demo flag — say so instead of letting
            # the standard flow take over silently.
            self.log(f"  ⚠ {ctx.task_id}: demo-flagged task but demo app "
                     f"generation is not enabled — running standard "
                     f"generation with the demo prompt appendix only")
            return
        if self._demo_app_generated_for == ctx.task_id:
            return
        self._demo_app_generated_for = ctx.task_id
        try:
            self.demo_app_generator(ctx)
        except Exception as e:  # noqa: BLE001 - implementer continues anyway
            self.log(f"  ⚠ demo app generation failed: {e} — implementer "
                     f"continues")

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
                feedback.parent.mkdir(parents=True, exist_ok=True)
                file_session_output(r, feedback)
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
            # FR-6.2: a crash between the merge checkpoint and the deploy
            # must still land the Pages deployment on resume — the deploy
            # runs before `complete()` here exactly as on the fresh path,
            # and a failure routes to `failed/` the same way (FR-8.1).
            deploy_failure = self._deploy_demo_app(ctx)
            if deploy_failure:
                self.lifecycle.fail(ctx.task_id,
                                    f"demo deployment failed: "
                                    f"{deploy_failure}")
                return "failed"
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
            deploy_failure = self._deploy_demo_app(ctx)
            if deploy_failure:
                # FR-8.1/FR-6.4: the source is already on trunk and is
                # not rolled back; the task itself goes to `failed/`
                # because the Pages deployment did not happen.
                self.lifecycle.fail(ctx.task_id,
                                    f"demo deployment failed: "
                                    f"{deploy_failure}")
                return "failed"
            self.lifecycle.complete(ctx.task_id, "Feature complete and merged to "
                         f"{self.cfg.trunk_branch}. " + _summary(r.output))
            self._cleanup_branch(ctx.workdir, ctx.task_id)
            return "done"
        self.lifecycle.park(ctx.task_id, "holistic review failed: " + _summary(r.output))
        return "parked"

    def _deploy_demo_app(self, ctx: StageContext) -> str:
        """Demo spec FR-6.2: run the final-deploy hook for a demo task.

        Returns "" on success or skip (hook unwired, non-demo task —
        FR-6.3: non-demo tasks never touch the deployer), and the hook's
        failure reason otherwise. A hook that ignores its never-raise
        contract is caught here: the failure routes the task, it cannot
        crash the harness.
        """
        if self.final_deploy_hook is None or not ctx.demo:
            return ""
        try:
            return self.final_deploy_hook(ctx) or ""
        except Exception as e:  # noqa: BLE001 - routed, not crashed
            self.log(f"  ⚠ final deploy hook raised: {e}")
            return str(e)

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

def file_session_output(r: SessionResult, dest: Path) -> None:
    """File one session's raw output as an artifact at `dest`.

    The pipeline used to `shutil.copy` the workdir `.out` capture directly.
    That capture is now cleaned up by the session layer once the transcript
    holds its contents (001-full-interactions-logged), so the copy falls back
    to the in-memory output — the capture was written from that same string,
    so both paths produce identical bytes. The destination artifact paths are
    unchanged (kickback reports, progress notes, review feedback).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = Path(r.out_file) if r.out_file is not None else None
    if src is not None and src.is_file():
        shutil.copy(src, dest)
    else:
        dest.write_text(r.output)


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