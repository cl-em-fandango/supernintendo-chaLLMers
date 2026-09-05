"""Slice-3 tests: the supervisor idles while an interrupt is active.

`interrupt --stand-down` (slice 1) only frees the model if a live supervisor
stops spawning. FR-6.3 is the supervisor's half of the contract:

  * the file is checked at the top of every cycle and inside the interruptible
    `_sleep()`, so a request landing mid-backoff (E6) and a `harness.py resume`
    landing mid-idle (FR-3.3) are both honored within ~1s;
  * while the file exists the loop spawns no child at all — not the status
    probe, not a work child — so the circuit breaker cannot trip on the idle
    period (and `harness.py status` itself still exits 0 while active);
  * one line is logged per state change, not per idle cycle;
  * idle cycles feed neither the no-progress backoff streak nor the breaker,
    and they do not consume a `MAX_CYCLES` slot: an interrupt expects a resume,
    not an exit;
  * a running child is never killed or preempted to honor an interrupt — the
    child stands down at its own session boundary (slice 2) and the supervisor
    only regains control when it exits;
  * the supervisor never acknowledges, transitions or clears the file: the
    bytes on disk are identical across an idle period (only no-arg `resume` or
    quick-mode completion may clear it), and a corrupt file idles the loop
    fail-safe with the recovery warning (E5).

Every fixture is a temp dir with the provider, lifecycle, tracker and (where
the sleep itself is not under test) `_sleep` stubbed — no containers, no real
`pi`, no real supervisor, no `/srv/pi-harness` writes (C2/C3).
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402
from harness.cli import handlers  # noqa: E402
from harness.core import interrupt as I  # noqa: E402
from harness.core.providers import Task  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402


class _FakeProvider:
    """Read-only id listings: pending work so a normal cycle has a child."""

    def __init__(self, pending: list[str]):
        self.pending = pending

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None) -> list[Task]:
        assert claim is False, "a counting call must never claim the queue"
        return [Task(id=tid, body="") for tid in self.pending]

    def list_claims(self) -> list[Task]:
        return []


class _FakeTracker:
    """Child stub recording every spawn and every kill attempt, in order."""

    def __init__(self, events: list[str], on_work=None):
        self.events = events
        self.on_work = on_work

    def spawn(self, args, *, label: str) -> int:
        self.events.append(f"spawn:{label}")
        if label != "status" and self.on_work:
            self.on_work()   # stands in for the child's whole session
        return 0

    def kill_tree(self) -> None:
        self.events.append("kill_tree")


class _SupervisorInterruptTest(unittest.TestCase):
    """`run_loop()` under a scripted interrupt file and scripted sleeps."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sup-int-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.events: list[str] = []
        self.slept: list[int] = []
        self.sleep_actions: list = []   # one callable per scripted sleep
        self.on_work = None

        def record_sleep(stop: dict, seconds: int) -> None:
            self.slept.append(seconds)
            step = len(self.slept) - 1
            if step < len(self.sleep_actions):
                self.sleep_actions[step]()
            else:
                stop["flag"] = True     # bounded run by default

        for patch in (
            mock.patch.object(S, "LOG", self.dir / "supervisor.log"),
            mock.patch.object(S, "STOPFILE", self.dir / "STOP"),
            mock.patch.object(S, "WORK_DIR", self.dir),
            mock.patch.object(S, "acquire_lock", lambda: True),
            mock.patch.object(S, "release_lock", lambda: None),
            mock.patch.object(S.signal, "signal", lambda *a, **k: None),
            mock.patch.object(S, "load", lambda path: mock.MagicMock()),
            mock.patch.object(S, "create_provider",
                              lambda cfg: _FakeProvider(["pending-0"])),
            mock.patch.object(S, "TaskLifecycle", lambda cfg, log=None: None),
            mock.patch.object(S, "in_flight_task_dirs",
                              lambda lifecycle: []),
            mock.patch.object(S, "SLEEP_S", 60),
            mock.patch.object(S, "MAX_SLEEP_S", 900),
            mock.patch.object(S, "FAIL_LIMIT", 99),
            mock.patch.object(S, "ChildTracker",
                              lambda: _FakeTracker(self.events, self.on_work)),
            mock.patch.object(S, "_sleep", record_sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    # --- helpers ---------------------------------------------------------
    def _request(self, mode=I.InterruptMode.STAND_DOWN,
                 state=I.InterruptState.REQUESTED) -> I.InterruptStatus:
        return I.write_interrupt(self.dir, mode, state)

    def _clear(self) -> None:
        I.clear_interrupt(self.dir)

    def _pause(self) -> None:
        I.acknowledge_interrupt(self.dir)

    def _stand_down_paused(self) -> None:
        """What a child leaves behind at its boundary: a `paused` record."""
        I.write_interrupt(self.dir, I.InterruptMode.STAND_DOWN,
                          I.InterruptState.PAUSED)

    def _run(self, cycles: int = 1) -> str:
        """Run the loop with the scripted fakes; return what it printed.

        `cycles` is the `MAX_CYCLES` budget; idle cycles do not consume it, so
        a run always ends through a scripted sleep (or the default stop).
        """
        with mock.patch.object(S, "MAX_CYCLES", cycles):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(S.run_loop(), 0)
        return out.getvalue()

    def _spawns(self) -> list[str]:
        """Every child the loop spawned, in order (the `finally` cleanup
        `kill_tree` is not a spawn and is asserted separately)."""
        return [event for event in self.events if event.startswith("spawn:")]

    def _idle_lines(self, output: str) -> list[str]:
        return [line for line in output.splitlines()
                if "interrupt active" in line]

    # --- file present at the cycle top -----------------------------------
    def test_an_active_file_spawns_no_child_at_all(self):
        self._request()
        out = self._run(cycles=1)
        self.assertEqual(self._spawns(), [],
                         "an idle supervisor must spawn no child, probe "
                         "included")
        self.assertEqual(self.slept, [60])
        self.assertNotIn("harness failed to launch", out)
        self.assertNotIn("no progress", out)

    def test_the_idle_line_names_mode_state_and_age_once(self):
        self._request()
        out = self._run(cycles=1)
        lines = self._idle_lines(out)
        self.assertEqual(len(lines), 1)
        self.assertIn("mode=STAND_DOWN", lines[0])
        self.assertIn("state=REQUESTED", lines[0])
        self.assertIn("age=", lines[0])
        self.assertIn("harness.py resume", lines[0])

    def test_the_file_survives_an_idle_period_untouched(self):
        """The supervisor reads the file; it never acks or clears it (FR-6.3).

        Acknowledgement belongs to the child at its session boundary; if the
        supervisor flipped `requested -> paused` the operator's request would
        be consumed by a process that is not standing work down.
        """
        self._request()
        before = I.interrupt_path(self.dir).read_bytes()
        self.sleep_actions = [lambda: None, lambda: None, lambda: None]
        self._run(cycles=1)
        self.assertEqual(I.interrupt_path(self.dir).read_bytes(), before,
                         "idle cycles must not rewrite the interrupt file")

    # --- one line per state change, not per cycle ------------------------
    def test_many_idle_cycles_log_one_line_per_state_change(self):
        self._request()
        self.sleep_actions = [lambda: None, lambda: None, lambda: None,
                              self._pause, lambda: None,
                              lambda: None]
        out = self._run(cycles=1)
        lines = self._idle_lines(out)
        # one sleep per idle cycle, plus the scripted stop
        self.assertEqual(len(self.slept), len(self.sleep_actions) + 1)
        self.assertEqual(len(lines), 2, f"one line per state change: {lines}")
        self.assertIn("state=REQUESTED", lines[0])
        self.assertIn("state=PAUSED", lines[1])
        self.assertEqual(self._spawns(), [])

    # --- the idle period is not "no progress" ----------------------------
    def test_idle_cycles_neither_back_off_nor_trip_the_breaker(self):
        self._request()
        self.sleep_actions = [lambda: None] * 5
        out = self._run(cycles=1)
        self.assertEqual(set(self.slept), {60},
                         "an idle cycle must not feed the backoff streak")
        self.assertNotIn("no progress", out)
        self.assertNotIn("harness failed to launch", out)
        self.assertNotIn("CIRCUIT BREAKER", out)

    def test_idle_cycles_do_not_consume_the_cycle_budget(self):
        """MAX_CYCLES bounds work cycles; an interrupt must not end the run.

        The interrupt keeps the supervisor alive for a resume (FR-5.1), so
        idling three times inside a `MAX_CYCLES=1` run still leaves a full
        normal cycle once the file is gone.
        """
        self._request()
        self.sleep_actions = [lambda: None, lambda: None, self._clear]
        out = self._run(cycles=1)
        self.assertEqual(self._spawns(),
                         ["spawn:status", "spawn:run-task-loop"])
        self.assertIn("── cycle 1:", out)

    # --- resume mid-sleep -------------------------------------------------
    def test_a_request_arriving_mid_sleep_is_honored_next_cycle(self):
        """E6 at loop level: the request lands during a sleep, not at a top."""
        self.sleep_actions = [self._request, lambda: None]
        out = self._run(cycles=1)
        self.assertEqual(self._spawns(),
                         ["spawn:status", "spawn:run-task-loop"])
        self.assertEqual(len(self._idle_lines(out)), 1)

    def test_a_resume_mid_sleep_resumes_the_normal_cycle(self):
        """FR-3.3/E8: one cleared file, one work child, no double spawn."""
        self._request()
        self.sleep_actions = [self._clear]
        out = self._run(cycles=1)
        self.assertEqual(self._spawns(),
                         ["spawn:status", "spawn:run-task-loop"])
        self.assertIn(S.INTERRUPT_CLEARED_LOG.strip(), out)
        self.assertEqual(out.count("interrupt cleared"), 1)

    def test_a_child_that_stood_down_is_not_counted_as_no_progress(self):
        """The child acks at its own boundary; the supervisor just idles after.

        The queue cannot move while the child stands down, so the identity
        test would otherwise start an operator-caused backoff — and a long
        backoff would delay the work `resume` is supposed to restart.
        """
        self.on_work = self._stand_down_paused
        self.sleep_actions = [lambda: None, self._clear]
        out = self._run(cycles=1)
        self.assertNotIn("no progress", out)
        self.assertEqual(set(self.slept), {60})
        self.assertIn("state=PAUSED", out)

    # --- the running child is left alone ---------------------------------
    def test_an_interrupt_never_kills_or_preempts_the_running_child(self):
        """FR-6.3: the child handles its own boundary; the loop never kills it.

        The request appears while the fake child is mid-session; the child
        runs to completion (rc 0), the loop then idles, and the only
        `kill_tree` is the `finally` cleanup after the loop has ended.
        """
        self.on_work = self._request
        self.sleep_actions = [lambda: None]
        out = self._run(cycles=1)
        self.assertEqual(self.events[:2],
                         ["spawn:status", "spawn:run-task-loop"])
        self.assertEqual(self.events[-1], "kill_tree",
                         "the only kill is the finally cleanup, after the loop")
        self.assertNotIn("kill_tree", self.events[:-1])
        self.assertNotIn("kill", out[:out.rfind("supervisor exited")])
        self.assertEqual(len(self._idle_lines(out)), 1)

    # --- corrupt file (E5) -------------------------------------------------
    def test_a_corrupt_file_idles_the_loop_fail_safe(self):
        path = I.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")
        out = self._run(cycles=1)
        self.assertEqual(self._spawns(), [])
        self.assertIn("mode=STAND_DOWN", out)
        self.assertIn(I.CORRUPT_INTERRUPT_WARNING, out)


class SleepWakesOnInterruptFileTest(unittest.TestCase):
    """The real `_sleep()`: presence of the file is an interrupt (E6, FR-3.3).

    These run the real sleep against a temp work dir with a background thread
    flipping the file, so the ~1s granularity is measured, not asserted away.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sup-int-sleep-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        patch = mock.patch.object(S, "WORK_DIR", self.dir)
        patch.start()
        self.addCleanup(patch.stop)

    def _change_after(self, change, delay: float) -> None:
        def later() -> None:
            time.sleep(delay)
            change()
        thread = threading.Thread(target=later, daemon=True)
        thread.start()

    def test_a_request_landing_mid_backoff_ends_the_sleep(self):
        self._change_after(lambda: self._request(), 0.2)
        start = time.monotonic()
        S._sleep({"flag": False}, 300)
        self.assertLess(time.monotonic() - start, 5)

    def test_a_resume_landing_mid_idle_ends_the_sleep(self):
        self._request()
        self._change_after(self._clear, 0.2)
        start = time.monotonic()
        S._sleep({"flag": False}, 300)
        self.assertLess(time.monotonic() - start, 5)

    def test_an_unchanged_file_sleeps_the_full_duration(self):
        start = time.monotonic()
        S._sleep({"flag": False}, 1)
        self.assertGreaterEqual(time.monotonic() - start, 0.9)

    def test_the_stop_flag_still_ends_the_sleep(self):
        start = time.monotonic()
        S._sleep({"flag": True}, 300)
        self.assertLess(time.monotonic() - start, 1)

    def _request(self) -> I.InterruptStatus:
        return I.write_interrupt(self.dir, I.InterruptMode.STAND_DOWN,
                                 I.InterruptState.REQUESTED)

    def _clear(self) -> None:
        I.clear_interrupt(self.dir)


class _StatusProvider:
    """The two read-only calls `cmd_status` makes about claims."""

    def list_claims(self) -> list[Task]:
        return []

    def claim_age_hours(self, task_id: str) -> float:
        return -1.0


class StatusProbeStaysGreenWhileInterruptedTest(unittest.TestCase):
    """FR-6.3: the breaker's probe must not fail because of an interrupt.

    The supervisor's circuit breaker counts failed `harness.py status`
    launches; if the probe went non-zero while the operator held the model,
    an idle period would end in a trunk revert.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sup-int-status-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        cfg = types.SimpleNamespace(queue_dir=self.dir,
                                    harness_execution_and_queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, _StatusProvider(),
                 None, lambda line="": None)
        patch = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patch.start()
        self.addCleanup(patch.stop)

    def test_status_exits_zero_with_an_active_interrupt(self):
        I.write_interrupt(self.dir, I.InterruptMode.STAND_DOWN,
                          I.InterruptState.PAUSED)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(handlers.cmd_status(), 0)

    def test_status_exits_zero_with_a_corrupt_interrupt_file(self):
        path = I.interrupt_path(self.dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json at all")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(handlers.cmd_status(), 0)


if __name__ == "__main__":
    unittest.main()
