"""T15 — the loop backs off when a cycle changed nothing, and resets when it did.

`run_loop()` re-reads the same three queue counts after the child exits that it
decided from before the child started: identical counts mean a cycle that
accomplished nothing, so the sleep doubles from `SLEEP_S` via
`backoff_seconds` and the streak is logged; any state change puts the sleep back
to exactly `SLEEP_S`. The breaker keeps its own `_sleep(stop, SLEEP_S)` and its
own reset (T06), so a failing-launch cycle stays out of the idle count. A
wedged task or an unreachable endpoint is idle, not an error: the loop backs
off, it never fails a task for sitting still.
"""
from __future__ import annotations

import importlib
import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402


class _Queue:
    """The three counts, mutable so a fake child can stand in for progress."""

    def __init__(self, pending: int = 1, in_flight: int = 0, claims: int = 0):
        self.pending = pending
        self.in_flight = in_flight
        self.claims = claims


class _FakeProvider:
    """Provider stub: only the read-only counting calls the loop makes."""

    def __init__(self, queue: _Queue):
        self.queue = queue

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None) -> list[str]:
        assert claim is False, "a counting call must never claim the queue"
        return ["task"] * self.queue.pending

    def list_claims(self) -> list[str]:
        return ["claim"] * self.queue.claims


class _FakeTracker:
    """Child stub: the status probe always launches, a work child runs `on_work`."""

    def __init__(self, on_work=None, fail_status: bool = False):
        self.on_work = on_work
        self.fail_status = fail_status

    def spawn(self, args, *, label: str) -> int:
        if label == "status":
            return 1 if self.fail_status else 0
        if self.on_work:
            self.on_work()
        return 0

    def kill_tree(self) -> None:
        pass


class LoopBackoffTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t15-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.queue = _Queue()
        self.slept: list[int] = []
        self.stops: list[dict] = []
        self.on_work = None
        self.fail_status = False

        def record_sleep(stop: dict, seconds: int) -> None:
            self.stops.append(stop)
            self.slept.append(seconds)

        def make_tracker() -> _FakeTracker:
            return _FakeTracker(lambda: self.on_work() if self.on_work else None,
                                fail_status=self.fail_status)

        for patch in (
            mock.patch.object(S, "LOG", self.dir / "supervisor.log"),
            mock.patch.object(S, "STOPFILE", self.dir / "STOP"),
            mock.patch.object(S, "acquire_lock", lambda: True),
            mock.patch.object(S, "release_lock", lambda: None),
            mock.patch.object(S.signal, "signal", lambda *a, **k: None),
            mock.patch.object(S, "load", lambda path: mock.MagicMock()),
            mock.patch.object(S, "create_provider",
                              lambda cfg: _FakeProvider(self.queue)),
            mock.patch.object(S, "TaskLifecycle", lambda cfg, log=None: None),
            mock.patch.object(S, "in_flight_task_dirs",
                              lambda lifecycle: ["task"] * self.queue.in_flight),
            mock.patch.object(S, "SLEEP_S", 60),
            mock.patch.object(S, "MAX_SLEEP_S", 900),
            mock.patch.object(S, "FAIL_LIMIT", 99),
            mock.patch.object(S, "ChildTracker", make_tracker),
            mock.patch.object(S, "_sleep", record_sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, cycles: int) -> str:
        """Run `cycles` supervised cycles with the fakes; return what was logged."""
        with mock.patch.object(S, "MAX_CYCLES", cycles):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(S.run_loop(), 0)
        return out.getvalue()

    def _scripted_progress(self, progressing_cycles: set[int]):
        """Make the work child move one task pending->active on the given cycles."""
        seen = {"n": 0}

        def work() -> None:
            seen["n"] += 1
            if seen["n"] in progressing_cycles:
                self.queue.pending -= 1
                self.queue.in_flight += 1

        self.on_work = work

    def test_idle_cycles_double_and_log_the_streak(self):
        out = self._run(4)
        self.assertEqual(self.slept, [120, 240, 480, 900])
        self.assertIn("no progress (streak 1); sleeping 120s", out)
        self.assertIn("no progress (streak 2); sleeping 240s", out)
        self.assertIn("no progress (streak 4); sleeping 900s", out)

    def test_a_changing_queue_sleeps_exactly_sleep_s(self):
        self._scripted_progress({1, 2, 3, 4})
        out = self._run(4)
        self.assertEqual(self.slept, [60, 60, 60, 60])
        self.assertNotIn("no progress", out)

    def test_progress_resets_the_streak(self):
        self._scripted_progress({1, 4})
        self._run(5)
        # idle counting restarts after the cycle-4 state change, not from 3
        self.assertEqual(self.slept, [60, 120, 240, 60, 120])

    def test_new_pending_from_generation_counts_as_progress(self):
        """An empty queue that generates work is progress, not a stall."""
        self.queue = _Queue(pending=0, in_flight=0, claims=0)

        def generate() -> None:
            self.queue.pending += 1

        self.on_work = generate
        self._run(2)
        self.assertEqual(self.slept, [60, 60])

    def test_backoff_sleep_gets_the_loops_stop_flag(self):
        """SIGTERM and the STOP file stay responsive: `_sleep` gets the flag."""
        seen: list[tuple[dict, int]] = []

        def stop_after_first(stop: dict, seconds: int) -> None:
            seen.append((stop, seconds))
            stop["flag"] = True

        with mock.patch.object(S, "_sleep", stop_after_first):
            with mock.patch.object(S, "MAX_CYCLES", 0):   # unlimited cycles
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(S.run_loop(), 0)
        self.assertEqual(len(seen), 1, "the loop did not stop on its own flag")
        self.assertEqual(seen[0][1], 120)
        self.assertTrue(seen[0][0]["flag"], "the stop dict handed to _sleep "
                                            "is not the one the loop watches")

    def test_a_failing_launch_keeps_the_breaker_sleep_and_the_streak(self):
        """The breaker path sleeps SLEEP_S and is not counted as idle."""
        self.fail_status = True
        out = self._run(3)
        self.assertEqual(self.slept, [60, 60, 60])
        self.assertIn("harness failed to launch", out)
        self.assertNotIn("no progress", out)


class MaxSleepConstantTest(unittest.TestCase):
    def test_cap_is_env_overridable(self):
        with mock.patch.dict(S.os.environ, {"SUPERVISOR_MAX_SLEEP_S": "123"}):
            importlib.reload(S)
        try:
            self.assertEqual(S.MAX_SLEEP_S, 123)
        finally:
            importlib.reload(S)
        self.assertEqual(S.MAX_SLEEP_S, 900)


if __name__ == "__main__":
    unittest.main()
