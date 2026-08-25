"""The task pipeline: spec -> feasibility -> slicing -> slices -> holistic."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .core import prompts
from .core.config import Config
from .core.gitops import ensure_branch, merge_to_trunk
from .core.providers import Task
from .core.session import SessionRunner


class Pipeline:
    def __init__(self, cfg: Config, runner: SessionRunner, log=print, provider=None):
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.provider = provider
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
    # task lifecycle
    # ------------------------------------------------------------------
    def task_dir(self, task_id: str, where: str = "active") -> Path:
        return self.cfg.queue_dir / where / task_id

    def intake(self, task: Task) -> Path:
        td = self.task_dir(task.id)
        (td / "artifacts" / "progress").mkdir(parents=True, exist_ok=True)
        (td / "prompts").mkdir(exist_ok=True)
        (td / "original.md").write_text(task.body)
        (td / "task.json").write_text(_json({
            "id": task.id,
            "status": "active",
            "source": task.source,
            "created": _now(),
            "stage": "spec",
            "history": [],
        }))
        return td

    def park(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "parked" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "PARKED", reason, "parked")
        self.log(f"  task {task_id} PARKED: {reason}")

    def fail(self, task_id: str, reason: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "failed" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "KICKED OUT", reason, "failed")
        self.log(f"  task {task_id} FAILED: {reason}")

    def complete(self, task_id: str, summary: str) -> None:
        src = self.task_dir(task_id)
        dst = self.cfg.queue_dir / "done" / task_id
        if src.exists():
            shutil.move(str(src), str(dst))
        self._exec_summary(task_id, "DONE", summary, "done")
        self.log(f"  task {task_id} DONE")

    def _exec_summary(self, task_id: str, status: str, text: str, where: str) -> None:
        td = self.cfg.queue_dir / where / task_id
        original = (td / "original.md").read_text() if (td / "original.md").exists() else ""
        review_dir = self.cfg.queue_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / f"{task_id}.md").write_text(f"""# Task: {task_id}

**Status:** {status}
**Date:** {_now()}

## Original requirement

{original}

## Executive summary

{text}

## Artifacts

- spec: `{td}/artifacts/spec.md`
- slices: `{td}/artifacts/slices.md`
- session outputs: `{td}/artifacts/*.out`
""")
        self.log(f"  exec summary: {review_dir / (task_id + '.md')}")

    # ------------------------------------------------------------------
    # workdir resolution
    # ------------------------------------------------------------------
    def resolve_workdir(self, td: Path) -> Path:
        """If the task references an existing git repo, work there; else the task dir."""
        for m in re.findall(r"/[a-zA-Z0-9_./-]+", (td / "original.md").read_text()):
            p = Path(m)
            if p.is_dir() and (p / ".git").exists():
                return p
        return td

    # ------------------------------------------------------------------
    # top level
    # ------------------------------------------------------------------
    def process(self, task: Task) -> str:
        td = self.intake(task)
        # Body is now persisted in the task dir; drop the pending/claim staging file
        # so this task cannot be re-claimed while it is in flight or terminal.
        if self.provider is not None and hasattr(self.provider, "release_claim"):
            self.provider.release_claim(task)
        self.log(f"═══ task {task.id} ═══")
        workdir = self.resolve_workdir(td)
        self.log(f"  workdir: {workdir}")
        try:
            ensure_branch(workdir, task.id, self.cfg.trunk_branch)
        except Exception as e:
            self.park(task.id, f"git setup failed: {e}")
            return "parked"

        if not self.stage_spec(task.id, td, workdir):
            return "parked"
        if not self.stage_feasibility(task.id, td, workdir):
            return "failed" if not td.exists() else "parked"
        if not self.stage_slicing(task.id, td, workdir):
            return "parked"
        if not self.stage_slices(task.id, td, workdir):
            return "parked"
        return self.stage_holistic(task.id, td, workdir)

    # ------------------------------------------------------------------
    # stage 1: specification
    # ------------------------------------------------------------------
    def stage_spec(self, tid: str, td: Path, workdir: Path) -> bool:
        kickbacks = 0
        while True:
            r = self._run(
                self.cfg.model, workdir, prompts.spec_author(td),
                task_id=tid, stage="spec_author")
            self.log(f"  spec author verdict: {r.verdict}")
            if r.verdict != "done":
                r = self._run(
                    self.cfg.model, workdir, prompts.spec_author(td),
                    task_id=tid, stage="spec_author", notes="retry")
                if r.verdict != "done":
                    self.park(tid, "spec author failed twice")
                    return False

            r = self._run(
                self.cfg.assessor, workdir, prompts.spec_assess(td, "ornith"),
                task_id=tid, stage="spec_assess_ornith")
            self.log(f"  ornith assessment verdict: {r.verdict}")
            if r.verdict == "kickback":
                kickbacks += 1
                if kickbacks > self.cfg.max_spec_kickbacks:
                    self.park(tid, f"spec kickback loop exceeded ({self.cfg.max_spec_kickbacks})")
                    return False
                shutil.copy(r.out_file, td / "artifacts" / f"kickback_ornith_{kickbacks}.md")
                self.log(f"  kickback to spec author (#{kickbacks})")
                continue

            r = self._run(
                self.cfg.model, workdir, prompts.spec_assess(td, "tw"),
                task_id=tid, stage="spec_assess_tw")
            self.log(f"  TW requirement-check verdict: {r.verdict}")
            if r.verdict == "kickback":
                kickbacks += 1
                if kickbacks > self.cfg.max_spec_kickbacks:
                    self.park(tid, f"spec kickback loop exceeded ({self.cfg.max_spec_kickbacks})")
                    return False
                shutil.copy(r.out_file, td / "artifacts" / f"kickback_tw_{kickbacks}.md")
                self.log(f"  kickback to spec author (#{kickbacks})")
                continue

            self.log("  spec approved")
            return True

    # ------------------------------------------------------------------
    # stage 2: feasibility
    # ------------------------------------------------------------------
    def stage_feasibility(self, tid: str, td: Path, workdir: Path) -> bool:
        r = self._run(
            self.cfg.implementer, workdir, prompts.feasibility(td),
            task_id=tid, stage="feasibility")
        self.log(f"  feasibility verdict: {r.verdict}")
        if r.verdict == "pass":
            return True
        if r.verdict == "kickout":
            self.fail(tid, "Task rejected at feasibility: " + _summary(r.output))
            return False
        if r.verdict == "kickback":
            shutil.copy(r.out_file, td / "artifacts" / "feasibility_kickback.md")
            self.log("  feasibility kickback -> back to spec stage")
            if not self.stage_spec(tid, td, workdir):
                return False
            r = self._run(
                self.cfg.implementer, workdir, prompts.feasibility(td),
                task_id=tid, stage="feasibility", notes="recheck")
            if r.verdict == "pass":
                return True
            self.park(tid, "feasibility still failing after spec revision")
            return False
        self.park(tid, f"feasibility verdict unclear: {r.verdict}")
        return False

    # ------------------------------------------------------------------
    # stage 3: slicing
    # ------------------------------------------------------------------
    def stage_slicing(self, tid: str, td: Path, workdir: Path) -> bool:
        r = self._run(
            self.cfg.implementer, workdir, prompts.slice(td),
            task_id=tid, stage="slicing")
        self.log(f"  slicing verdict: {r.verdict}")
        if r.verdict != "done":
            self.park(tid, f"slicing failed (verdict={r.verdict})")
            return False

        fast = self.cfg.fast_pool[0] if self.cfg.fast_pool else self.cfg.implementer
        for check in range(1, self.cfg.max_slice_check_loops + 1):
            r = self._run(
                fast, workdir, prompts.slice_check(td),
                task_id=tid, stage="slice_check", iteration=check)
            self.log(f"  slice check #{check} verdict: {r.verdict}")
            if r.verdict == "pass":
                return True
            if r.verdict == "resliced":
                continue
        self.park(tid, "slice fit check loop exceeded")
        return False

    # ------------------------------------------------------------------
    # stage 4: per-slice execution
    # ------------------------------------------------------------------
    def stage_slices(self, tid: str, td: Path, workdir: Path) -> bool:
        slices = _parse_slices(td / "artifacts" / "slices.md")
        if not slices:
            self.park(tid, "no slices parsed from slices.md")
            return False
        self.log(f"  slices: {' '.join(slices)}")

        for sid in slices:
            self.log(f"  ── slice {sid} ──")
            if not self._implement(tid, td, workdir, sid):
                return False
            if not self._review_loop(tid, td, workdir, sid, "tech"):
                return False
            if not self._review_loop(tid, td, workdir, sid, "func"):
                return False
            self.log(f"    slice {sid} passed all reviews")
        return True

    def _implement(self, tid: str, td: Path, workdir: Path, sid: str) -> bool:
        for it in range(1, self.cfg.max_slice_implement + 1):
            r = self._run(
                self.cfg.implementer, workdir,
                prompts.implement_slice(td, sid, it, self.cfg.max_slice_implement),
                task_id=tid, stage="slice_implement", slice_id=sid, iteration=it)
            self.log(f"    implement iter {it} verdict: {r.verdict}")
            if r.verdict == "done":
                return True
            note = td / "artifacts" / "progress" / f"slice-{sid}.md"
            if r.verdict == "progress" and not note.exists():
                shutil.copy(r.out_file, note)
        self.park(tid, f"slice {sid} not delivered in {self.cfg.max_slice_implement} implementation iterations")
        return False

    def _review_loop(self, tid: str, td: Path, workdir: Path, sid: str, kind: str) -> bool:
        max_iter = (self.cfg.max_slice_tech_review if kind == "tech"
                    else self.cfg.max_slice_func_review)
        model = self.cfg.implementer if kind == "tech" else self.cfg.model
        prompt_fn = prompts.tech_review if kind == "tech" else prompts.func_review
        stage = f"{kind}_review"

        for it in range(1, max_iter + 1):
            r = self._run(
                model, workdir, prompt_fn(td, sid),
                task_id=tid, stage=stage, slice_id=sid, iteration=it)
            self.log(f"    {kind} review iter {it} verdict: {r.verdict}")
            if r.verdict == "pass":
                return True
            if it < max_iter:
                feedback = td / "artifacts" / "progress" / f"slice-{sid}.md"
                shutil.copy(r.out_file, feedback)
                self._run(
                    model, workdir, prompts.fix_slice(td, sid, feedback, kind),
                    task_id=tid, stage="slice_fix", slice_id=sid, iteration=it,
                    notes=f"fix after {kind} review")
        self.park(tid, f"slice {sid} failed {kind} review after {max_iter} iterations")
        return False

    # ------------------------------------------------------------------
    # stage 5: holistic review + merge
    # ------------------------------------------------------------------
    def stage_holistic(self, tid: str, td: Path, workdir: Path) -> str:
        r = self._run(
            self.cfg.model, workdir, prompts.holistic_review(td),
            task_id=tid, stage="holistic")
        self.log(f"  holistic review verdict: {r.verdict}")
        if r.verdict == "pass":
            try:
                title = (td / "original.md").read_text().strip().splitlines()[0][:70]
                merge_to_trunk(workdir, tid, self.cfg.trunk_branch, title)
            except Exception as e:
                self.park(tid, f"merge failed: {e}")
                return "parked"
            self.complete(tid, "Feature complete and merged to "
                         f"{self.cfg.trunk_branch}. " + _summary(r.output))
            return "done"
        self.park(tid, "holistic review failed: " + _summary(r.output))
        return "parked"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(obj) -> str:
    import json
    return json.dumps(obj, indent=2)
