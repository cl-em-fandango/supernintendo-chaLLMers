"""T15 — the no-progress backoff math, and the sleep it feeds.

`backoff_seconds` is the pure half of the loop's answer to a cycle that
accomplished nothing: streak 0 keeps today's `SLEEP_S`, every further idle
cycle doubles, and `MAX_SLEEP_S` caps it so a wedged task or an unreachable
model endpoint costs a shrinking share of the CPU and log volume instead of a
full probe-and-spawn every 60 s forever. The doubling is worth nothing if the
resulting sleep ignores a stop, so the interruptibility of `_sleep` is asserted
here too, at backoff length.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402
from harness.workflow.cycle import QueueCounts, backoff_seconds  # noqa: E402


class BackoffSecondsTest(unittest.TestCase):
    def test_streak_zero_is_the_plain_sleep(self):
        """A healthy loop must be unchanged: streak 0 returns base."""
        self.assertEqual(backoff_seconds(0, 60, 900), 60)

    def test_every_idle_cycle_doubles(self):
        self.assertEqual(backoff_seconds(1, 60, 900), 120)
        self.assertEqual(backoff_seconds(2, 60, 900), 240)
        self.assertEqual(backoff_seconds(3, 60, 900), 480)

    def test_growth_is_capped(self):
        self.assertEqual(backoff_seconds(4, 60, 900), 900)
        self.assertEqual(backoff_seconds(10, 60, 900), 900)
        self.assertEqual(backoff_seconds(1000, 60, 900), 900,
                         "a long streak must not explode past the cap")

    def test_never_exceeds_the_cap_for_any_streak(self):
        for streak in range(0, 20):
            self.assertLessEqual(backoff_seconds(streak, 60, 900), 900)

    def test_is_monotonic_in_the_streak(self):
        sleeps = [backoff_seconds(n, 60, 900) for n in range(12)]
        self.assertEqual(sleeps, sorted(sleeps))

    def test_a_cap_below_base_is_honoured_not_amplified(self):
        self.assertEqual(backoff_seconds(0, 60, 30), 30)

    def test_negative_streak_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            backoff_seconds(-1, 60, 900)
        self.assertIn("idle_streak", str(ctx.exception))


class QueueCountsTest(unittest.TestCase):
    """The progress test is `after != before`, so equality must be by value."""

    def test_equal_counts_are_equal(self):
        self.assertEqual(QueueCounts(1, 2, 3), QueueCounts(1, 2, 3))

    def test_any_state_change_differs(self):
        base = QueueCounts(1, 2, 3)
        self.assertNotEqual(base, QueueCounts(0, 2, 3))   # pending worked
        self.assertNotEqual(base, QueueCounts(1, 3, 3))   # a claim went active
        self.assertNotEqual(base, QueueCounts(1, 2, 2))   # a claim consumed
        self.assertNotEqual(base, QueueCounts(2, 2, 3))   # generation added work

    def test_a_snapshot_cannot_be_mutated(self):
        counts = QueueCounts(1, 2, 3)
        with self.assertRaises(Exception):
            counts.pending = 0  # type: ignore[misc]


class SleepStaysInterruptibleTest(unittest.TestCase):
    """A backoff-length sleep must still answer a stop immediately (D5)."""

    def _sleep_stop_dict(self, seconds: int) -> float:
        start = time.monotonic()
        S._sleep({"flag": True}, seconds)
        return time.monotonic() - start

    def test_returns_immediately_when_stop_is_already_set(self):
        self.assertLess(self._sleep_stop_dict(900), 1.0,
                        "a set stop flag did not cut the backoff short")

    def test_returns_early_when_stop_is_set_mid_sleep(self):
        stop = {"flag": False}
        timer = threading.Timer(0.2, lambda: stop.__setitem__("flag", True))
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        S._sleep(stop, 600)
        self.assertLess(time.monotonic() - start, 2.0,
                        "_sleep ignored a stop raised mid-backoff")

    def test_the_loop_never_calls_time_sleep_directly(self):
        """Everything sleeps through `_sleep`, so SIGTERM stays responsive."""
        src = Path(S.__file__).read_text()
        loop_body = src.split("def _sleep")[0]
        self.assertNotIn("time.sleep(", loop_body)


if __name__ == "__main__":
    unittest.main()
