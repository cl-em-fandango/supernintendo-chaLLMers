"""Slice-2 tests: the file halts harness work at a session boundary.

An interrupt request (`interrupt --stand-down`, slice 1) is only honored if
the harness child acts on it. Two shapes of child exist:

  * single-session commands — `run`, `run-one`, `run-task`, `resume <task_id>`
    — have no in-run boundary, so honoring the file (FR-5.4, FR-3.1) means
    taking no work at all: return 0 without claiming or spawning, and leave
    the file byte-identical (only no-arg `resume` or quick-mode completion
    may clear it);
  * run loops — `run-task-loop`, `autonomous` — reach real session
    boundaries between tasks/attempts. At a boundary (FR-6.1: immediately
    before spawning the next `pi` session) the loop acknowledges the request
    (`requested -> paused`, FR-6.2) and unwinds: exit 0, no parking, no
    crash-retry, in-flight tasks stay in `active/` with their checkpoints
    and claims (FR-6.4/FR-6.5).

Covered here:
  * `acknowledge_interrupt` in `interrupt.py`: no file -> None and nothing
    created; `requested -> paused` preserving mode/`requested_at`/
    `requester_pid` and refreshing `updated_at`; an already-`paused` file
    returned unchanged, nothing rewritten; a corrupt file acknowledged as a
    clean STAND_DOWN/PAUSED record (fail-safe, FR-5.3);
  * each single-session command returns 0 with no claim, no pipeline work
    and no `resume_task` call for every mode/state combination, and for a
    corrupt file, and the file on disk is byte-identical before and after;
  * with no file the same commands work exactly as before (the guard does
    not block the normal path);
  * `cmd_run_task_loop` with a file already active: acknowledges at the
    start and never claims; with a request arriving mid-session: the running
    session finishes, the loop exits 0, the file shows `paused`, no second
    session is spawned, the in-flight task keeps its claim (with sidecar)
    and its `active/` checkpoint, nothing is parked, and the untouched task
    stays in `pending/` (AC1/AC2/AC4 partial);
  * `cmd_autonomous` with a file active never constructs the generator;
    `AutonomousGenerator.run` consults `stand_down_check` at each attempt
    boundary *and* between the suggest and review sessions of one attempt,
    stopping spawning when it answers True;
  * the real `Pipeline` waterfall: `Pipeline._run_attempts` consults
    `stand_down_check` immediately before every `runner.run` dispatch, so a
    request arriving mid-waterfall stops all further sessions; `process`
    returns `stood_down` without parking (FR-6.1/FR-6.2/FR-6.4);
  * `cmd_run`'s loop checks the boundary at its top and hands
    `stand_down_check` to the autonomous generator it may enter;
  * `acknowledge_interrupt` against a concurrent `resume` (TOCTOU): a delete
    landing between the read and the write is not resurrected as `paused`.

Every fixture is a temp dir with `build()` patched and the pipeline/runner
stubbed — no containers, no real `pi`, no `/srv/pi-harness` writes (C2/C3).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import types
import unittest
from itertools import product
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core import interrupt  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.enums import Stage, Verdict  # noqa: E402
from harness.core.providers import DirectoryTaskProvider, Task  # noqa: E402
from harness.core.session import SessionResult  # noqa: E402
from harness.core.stand_down import StandDownWatcher  # noqa: E402
from harness.workflow.autonomous import AutonomousGenerator  # noqa: E402
from harness.workflow.pipeline import Pipeline  # noqa: E402

MODES = (interrupt.InterruptMode.STAND_DOWN, interrupt.InterruptMode.QUICK)
STATES = (interrupt.InterruptState.REQUESTED, interrupt.InterruptState.PAUSED)


class _TempWorkDir(unittest.TestCase):
    """Shared temp workDir; nothing here resolves config.json."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-b-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)


class TestAcknowledgeInterrupt(_TempWorkDir):
    """The transition helper in `interrupt.py` — the loops' only ack path."""

    def test_no_file_returns_none_and_creates_nothing(self):
        self.assertIsNone(interrupt.acknowledge_interrupt(self.dir))
        self.assertFalse(interrupt.interrupt_path(self.dir).exists())

    def test_requested_transitions_to_paused_preserving_the_request(self):
        written = interrupt.write_interrupt(
            self.dir, interrupt.InterruptMode.QUICK,
            interrupt.InterruptState.REQUESTED, requester_pid=4242)
        status = interrupt.acknowledge_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertEqual(status.mode, interrupt.InterruptMode.QUICK)
        self.assertEqual(status.requested_at, written.requested_at)
        self.assertEqual(status.requester_pid, 4242)
        self.assertEqual(status, interrupt.read_interrupt(self.dir))

    def test_already_paused_is_reported_and_not_rewritten(self):
        interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                  interrupt.InterruptState.PAUSED)
        before = interrupt.interrupt_path(self.dir).read_bytes()
        status = interrupt.acknowledge_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertEqual(interrupt.interrupt_path(self.dir).read_bytes(), before)

    def test_corrupt_file_is_acknowledged_as_a_clean_paused_record(self):
        # FR-5.3 fail-safe: the corrupt file still means "stand down", so the
        # ack lands it as a readable PAUSED record instead of leaving garbage.
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")
        status = interrupt.acknowledge_interrupt(self.dir)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertEqual(interrupt.read_interrupt(self.dir), status)

    def test_resume_deleting_mid_ack_is_not_resurrected_as_paused(self):
        """TOCTOU: a `resume` delete between the ack's read and write wins.

        The delete is simulated by making the file vanish for the ack's
        re-read (before the write) and for its post-write verification:
        a harness the operator just released must not be re-paused by a
        late ack, and with no file the ack reports "no interrupt" (E8).
        """
        real_read = interrupt.read_interrupt
        for gone_at in (2, 3):  # the delete lands before write / before verify
            with self.subTest(gone_at=gone_at):
                interrupt.write_interrupt(
                    self.dir, interrupt.InterruptMode.STAND_DOWN,
                    interrupt.InterruptState.REQUESTED)
                reads: list[int] = []

                def racing_read(work_dir, log=None):
                    reads.append(1)
                    if len(reads) == gone_at:
                        interrupt.interrupt_path(work_dir).unlink()
                        return None
                    return real_read(work_dir, log=log)

                with mock.patch.object(interrupt, "read_interrupt",
                                       racing_read):
                    self.assertIsNone(
                        interrupt.acknowledge_interrupt(self.dir),
                        "a resumed request must read as no interrupt")
                self.assertFalse(interrupt.interrupt_path(self.dir).exists(),
                                 "the ack resurrected a resumed request")


class _RecordingPipeline:
    """Pipeline stub: records sessions, and can act mid-"session".

    `on_process` hooks run inside `process`, which stands for the one `pi`
    session the real pipeline would spawn for the task — that is where a
    mid-session interrupt request becomes visible to the next boundary.
    """

    lifecycle = None

    def __init__(self) -> None:
        self.processed: list[str] = []
        self.on_process = None  # callable(task) -> None

    def process(self, task: Task) -> None:
        self.processed.append(task.id)
        if self.on_process is not None:
            self.on_process(task)


class _BoundaryFixture(unittest.TestCase):
    """Temp queue dirs, the real directory provider, stubbed `build()`.

    cfg carries a `work_dir` (where the interrupt file lives) alongside the
    queue dirs; the stale-claim guard reads its flag defensively, so a cfg
    without `.get()` means "off", and the autonomous hand-off is made inert.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-loop-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.active = self.dir / "active"
        self.parked = self.dir / "parked"
        for d in (self.pending, self.claimed, self.active, self.parked):
            d.mkdir()
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)
        self.pipeline = _RecordingPipeline()
        cfg = types.SimpleNamespace(work_dir=self.dir, queue_dir=self.dir,
                                    logs_dir=self.dir / "logs")
        wired = (cfg, None, None, self.provider, self.pipeline,
                 lambda line="": self.messages.append(line))
        self._patch(handlers, "build", lambda *a, **k: wired)

        class _NoopGenerator:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs) -> int:
                return 0

        self._patch(handlers, "AutonomousGenerator", _NoopGenerator)
        self.resume_calls: list = []
        self._patch(handlers, "resume_task",
                    lambda *a, **k: self.resume_calls.append((a, k)) or 0)

    def _patch(self, target, name: str, replacement) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.pending / name).write_text(f"# {name[:-3]}\nwork on {name}\n")

    def _logged(self) -> str:
        return " | ".join(self.messages)

    def _write_interrupt(self, mode=interrupt.InterruptMode.STAND_DOWN,
                         state=interrupt.InterruptState.REQUESTED) -> bytes:
        interrupt.write_interrupt(self.dir, mode, state)
        return interrupt.interrupt_path(self.dir).read_bytes()


class TestSingleSessionGuards(_BoundaryFixture):
    """2.1: `run`/`run-one`/`run-task`/`resume <id>` take no work, keep the file."""

    def _commands(self) -> dict:
        missing_task = self.dir / "never-read.md"
        return {
            "run": handlers.cmd_run,
            "run-one": handlers.cmd_run_one,
            # The task file does not exist: reading it would raise, so a
            # return of 0 proves the guard fired before any work.
            "run-task": lambda: handlers.cmd_run_task(str(missing_task)),
            "resume <id>": lambda: handlers.cmd_resume("001-a"),
        }

    def test_each_command_takes_no_work_and_leaves_the_file_untouched(self):
        for name, call in self._commands().items():
            with self.subTest(command=name):
                before = self._write_interrupt()
                self.assertEqual(call(), 0, f"{name} did not stand down")
                self.assertEqual(self.pipeline.processed, [])
                self.assertEqual(self.resume_calls, [])
                self.assertEqual(self._claimed_names(), [])
                self.assertEqual(
                    interrupt.interrupt_path(self.dir).read_bytes(), before,
                    f"{name} modified the interrupt file")
                self.assertIn("interrupt active", self._logged())

    def test_every_mode_and_state_combination_blocks(self):
        for mode, state in product(MODES, STATES):
            with self.subTest(mode=mode.name, state=state.name):
                interrupt.write_interrupt(self.dir, mode, state)
                self._seed("001-a.md")
                self.assertEqual(handlers.cmd_run(), 0)
                self.assertEqual(self.pipeline.processed, [])
                self.assertEqual(self._pending_names(), ["001-a.md"])

    def test_corrupt_file_takes_no_work_fail_safe(self):
        # FR-5.3: garbage reads as STAND_DOWN/REQUESTED, so the guard fires.
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{ broken")
        before = path.read_bytes()
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self.pipeline.processed, [])
        self.assertEqual(path.read_bytes(), before)

    def test_no_file_the_commands_work_as_before(self):
        # The guard must not block the normal path: run works its claim.
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a"])
        self.assertEqual(handlers.cmd_resume("001-a", yes=True), 0)
        self.assertEqual(len(self.resume_calls), 1)

    def _pending_names(self) -> list[str]:
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.md"))


class TestRunLoopBoundary(_BoundaryFixture):
    """2.2: the loops ack at a boundary and unwind with checkpoints intact."""

    def test_active_file_at_loop_start_acks_before_claiming_anything(self):
        before = self._write_interrupt()
        self._seed("001-a.md", "002-b.md")
        self.assertEqual(handlers.cmd_run_task_loop(), 0)
        self.assertEqual(self.pipeline.processed, [], "the loop took work")
        self.assertEqual(self._claimed_names(), [], "the loop claimed")
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertIn("stood down at session boundary", self._logged())
        # The untouched tasks stay where they were (FR-6.2).
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertNotEqual(
            interrupt.interrupt_path(self.dir).read_bytes(), before,
            "the ack did not reach the file")

    def test_request_mid_session_stops_the_loop_at_the_next_boundary(self):
        """AC1 partial: session finishes, loop exits 0, file paused, no respawn."""
        self._seed("001-a.md", "002-b.md")

        def arrive_mid_session(task: Task) -> None:
            # The operator requests while this task's session is running; the
            # task is in flight: claimed, with an active/ checkpoint.
            interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                      interrupt.InterruptState.REQUESTED)
            task_record.set_claim(self.dir, task.id,
                                  "run-task-loop-mock-owner")
            checkpoint = self.active / task.id
            checkpoint.mkdir()
            (checkpoint / "checkpoint.json").write_text(
                json.dumps({"stage": "implement", "slice": 1}) + "\n")

        self.pipeline.on_process = arrive_mid_session
        self.assertEqual(handlers.cmd_run_task_loop(), 0)

        self.assertEqual(self.pipeline.processed, ["001-a"],
                         "a second session was spawned after the request")
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.mode, interrupt.InterruptMode.STAND_DOWN)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        # The in-flight task keeps its claim and its checkpoint (FR-6.4).
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        held = task_record.read_record(self.dir, "001-a").claim
        self.assertEqual(held.owner if held else None,
                         "run-task-loop-mock-owner")
        self.assertEqual(
            json.loads((self.active / "001-a" / "checkpoint.json").read_text()),
            {"stage": "implement", "slice": 1})
        # Nothing parked, no crash-retry path taken, next task untouched.
        self.assertEqual(self._parked_names(), [])
        self.assertEqual(self._pending_names(), ["002-b.md"])
        self.assertIn("stood down at session boundary", self._logged())

    def test_cmd_run_loop_stands_down_at_its_top_too(self):
        """FR-5.4 lists `run`: a request mid-task stops the loop, claims back.

        `cmd_run` is a run loop, not a single session: after the in-flight
        task the loop top is a boundary — it acks, returns 0 through the
        claim hand-back, and never reaches the autonomous hand-off.
        """
        self._seed("001-a.md", "002-b.md")

        def arrive_mid_task(task: Task) -> None:
            interrupt.write_interrupt(self.dir, interrupt.InterruptMode.STAND_DOWN,
                                      interrupt.InterruptState.REQUESTED)

        self.pipeline.on_process = arrive_mid_task
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a"],
                         "the loop took work after the request")
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertIn("stood down at session boundary", self._logged())
        # The hand-back ran: this run claims nothing on its way out.
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._parked_names(), [])

    def test_cmd_run_hands_the_boundary_check_to_autonomous(self):
        """The `run` -> autonomous hand-off must not be check-blind."""
        captured: dict = {}

        class _CapturingGenerator:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs) -> int:
                captured.update(kwargs)
                return 0

        patcher = mock.patch.object(handlers, "AutonomousGenerator",
                                    _CapturingGenerator)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(handlers.cmd_run(), 0)  # pending starts empty
        check = captured.get("stand_down_check")
        self.assertTrue(callable(check),
                        "cmd_run entered autonomous without stand_down_check")
        self.assertFalse(check())
        self._write_interrupt()
        self.assertTrue(check(), "the handed check does not see the file")
        self.assertIn("stood down at session boundary", self._logged())

    def test_corrupt_file_at_loop_boundary_pauses_and_stops(self):
        path = interrupt.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("garbage")
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run_task_loop(), 0)
        self.assertEqual(self.pipeline.processed, [])
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)

    def _pending_names(self) -> list[str]:
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def _parked_names(self) -> list[str]:
        return sorted(p.name for p in self.parked.glob("*"))


class TestAutonomousBoundary(unittest.TestCase):
    """`autonomous`: the top guard, and the per-attempt boundary in the loop."""

    def test_cmd_autonomous_with_active_file_never_builds_the_generator(self):
        work = Path(tempfile.mkdtemp(prefix="interrupt-auto-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        messages: list[str] = []
        cfg = types.SimpleNamespace(work_dir=work, queue_dir=work,
                                    logs_dir=work / "logs")
        wired = (cfg, None, None, None, None,
                 lambda line="": messages.append(line))
        built: list = []

        class _NeverGenerator:
            def __init__(self, *args, **kwargs):
                built.append(args)

            def run(self, *args, **kwargs) -> int:
                raise AssertionError("generator ran despite the interrupt")

        with mock.patch.object(handlers, "build", lambda *a, **k: wired), \
                mock.patch.object(handlers, "AutonomousGenerator", _NeverGenerator):
            interrupt.write_interrupt(work, interrupt.InterruptMode.STAND_DOWN,
                                      interrupt.InterruptState.REQUESTED)
            self.assertEqual(handlers.cmd_autonomous(), 0)
        self.assertEqual(built, [], "autonomous started despite the interrupt")
        status = interrupt.read_interrupt(work)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertIn("stood down at session boundary", " | ".join(messages))

    def test_generator_stops_between_suggest_and_review(self):
        """One attempt is two sessions: the review spawn is a boundary too."""
        work = Path(tempfile.mkdtemp(prefix="interrupt-auto3-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        (work / "queue" / "pending").mkdir(parents=True)
        cfg = types.SimpleNamespace(autonomous_queue_target=1,
                                    fast_pool=["m1", "m2"],
                                    random_pool=["m1", "m2"],
                                    work_dir=work,
                                    queue_dir=work / "queue")
        stages: list = []

        class _SuggestPassRunner:
            def run(self, *args, stage=None, **kwargs) -> SessionResult:
                stages.append(stage)
                output = ("Feature proposal\n\n## Summary\nok\n\n"
                          "VERDICT: done")
                return SessionResult(ok=True, verdict=Verdict.DONE,
                                     peak_tokens=0, duration_s=0.0,
                                     output=output, out_file=work / "out")

        checks = iter([False, True])  # attempt top: go; before review: stop
        messages: list[str] = []
        provider = types.SimpleNamespace(count_pending=lambda: 0)
        gen = AutonomousGenerator(cfg, _SuggestPassRunner(), provider,
                                  log=messages.append)
        added = gen.run(work, stand_down_check=lambda: next(checks, True))
        self.assertEqual(stages, [Stage.AUTONOMOUS_SUGGEST],
                         "the review session spawned past the request")
        self.assertEqual(added, 0)
        self.assertEqual(list((work / "queue" / "pending").glob("*.md")), [],
                         "a task was queued after the stand-down")
        self.assertIn("stood down at session boundary", " | ".join(messages))

    def test_generator_stops_at_the_attempt_boundary(self):
        """`stand_down_check` is consulted before each attempt's first spawn."""
        work = Path(tempfile.mkdtemp(prefix="interrupt-auto2-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        (work / "queue" / "pending").mkdir(parents=True)
        cfg = types.SimpleNamespace(autonomous_queue_target=2,
                                    fast_pool=["m1", "m2"],
                                    random_pool=["m1", "m2"],
                                    work_dir=work,
                                    queue_dir=work / "queue")
        calls: list[int] = []

        class _StubRunner:
            def run(self, *args, **kwargs) -> SessionResult:
                calls.append(1)
                # A non-DONE verdict makes the attempt retry, so the second
                # boundary check is reached without any real session.
                return SessionResult(ok=False, verdict=Verdict.FAIL,
                                     peak_tokens=0, duration_s=0.0,
                                     output="", out_file=work / "out")

        checks = iter([False, True])
        messages: list[str] = []
        provider = types.SimpleNamespace(count_pending=lambda: 0)
        gen = AutonomousGenerator(cfg, _StubRunner(), provider,
                                  log=messages.append)
        added = gen.run(work, stand_down_check=lambda: next(checks, True))
        self.assertEqual(added, 0)
        self.assertEqual(len(calls), 1,
                         "the generator spawned past a True stand_down_check")
        self.assertIn("stood down at session boundary", " | ".join(messages))


class TestPipelineWaterfallBoundary(unittest.TestCase):
    """The real waterfall stops before every `runner.run` dispatch.

    `Pipeline.process` runs the whole stage waterfall — many `pi` sessions
    per task — so a per-task boundary check alone lets a request arriving
    early in a task spawn session after session. `Pipeline._run_attempts`
    is the single choke point immediately before each dispatch; pinned here
    with the real Pipeline and a fake runner that counts dispatches.
    """

    def setUp(self):
        from tests.test_all_attempts_crashed import _cfg, _make_repo
        self.dir = Path(tempfile.mkdtemp(prefix="interrupt-pipe-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.queue_dir = self.dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = _make_repo(self.dir / "repo")
        self.cfg = _cfg(self.dir, repo=self.repo)
        self.messages: list[str] = []

    def _task(self) -> Task:
        return Task(id="t1", body="# t1\nwork\n", source="directory:t1.md")

    def test_request_mid_waterfall_stops_every_further_dispatch(self):
        from tests.test_all_attempts_crashed import ScriptedRunner

        runner = ScriptedRunner()
        original_run = runner.run

        def arriving_run(*args, **kwargs):
            result = original_run(*args, **kwargs)
            # The operator requests while this session is running.
            interrupt.write_interrupt(self.dir,
                                      interrupt.InterruptMode.STAND_DOWN,
                                      interrupt.InterruptState.REQUESTED)
            return result

        runner.run = arriving_run
        pipeline = Pipeline(self.cfg, runner, log=self.messages.append,
                            stand_down_check=StandDownWatcher(
                                self.dir, log=self.messages.append))
        outcome = pipeline.process(self._task())
        self.assertEqual(outcome, "stood_down")
        self.assertEqual(len(runner.calls), 1,
                         "the waterfall spawned past the request")
        # FR-6.2/FR-6.4: no parking, no crash-retry; FR-6.5: the task stays
        # in active/ with its checkpoints.
        self.assertEqual(list((self.queue_dir / "parked").glob("*")), [])
        self.assertTrue((self.queue_dir / "active" / "t1").is_dir())
        status = interrupt.read_interrupt(self.dir)
        self.assertEqual(status.state, interrupt.InterruptState.PAUSED)
        self.assertIn("stood down at session boundary",
                      " | ".join(self.messages))

    def test_check_active_before_the_first_dispatch_stops_before_any_session(self):
        """The gate runs ahead of every dispatch, including the stage's first."""
        from tests.test_all_attempts_crashed import ScriptedRunner
        runner = ScriptedRunner()
        pipeline = Pipeline(self.cfg, runner, log=self.messages.append,
                            stand_down_check=lambda: True)
        outcome = pipeline.process(self._task())
        self.assertEqual(outcome, "stood_down")
        self.assertEqual(runner.calls, [], "a session spawned past the gate")
        self.assertEqual(list((self.queue_dir / "parked").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
