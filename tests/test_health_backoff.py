"""FR-5.1 sub-slice 2.2: the bounded exponential backoff loop.

`wait_for_healthy_server` probes, and on failure sleeps base * 2^(n-1)
capped at `backoff_cap_s`, up to `max_attempts` probes. The clock and the
transport are injected here, so the whole schedule is observed without
wall-clock time and without a server (NFR-4).

Pinned behavior:
- healthy on the first probe: no sleep, no log line;
- healthy on a later probe: exactly the sleeps before the success;
- exhausted: a distinct UNHEALTHY outcome (not a crash), attempts ==
  max_attempts, and the greppable NFR-3 lines `LLM-HEALTH-BACKOFF` /
  `LLM-HEALTH-EXHAUSTED`;
- disabled: immediate DISABLED gate — no probe, no sleep, no log.

Run from the repo root:  python3 -m unittest tests.test_health_backoff
"""
from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.health import (
    HealthGate,
    HealthOutcome,
    HealthPolicy,
    backoff_delay_s,
    wait_for_healthy_server,
)


class FakeClock:
    def __init__(self):
        self.sleeps: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _fake_transport(failures_before_success: int | None):
    """An `open_url` stand-in: fails the first N probes, then succeeds.

    `None` for N means "always fail". Failures are raised the way a real
    refused connection surfaces (URLError), so the probe path under test
    is the real exception path, not a stubbed bool.
    """
    state = {"probes": 0}

    def open_url(request, timeout=None):
        state["probes"] += 1
        if failures_before_success is None or state["probes"] <= failures_before_success:
            raise urllib.error.URLError("connection refused")
        return _OkResponse()

    open_url.state = state
    return open_url


class _OkResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _policy(**overrides) -> HealthPolicy:
    fields = dict(url="http://fake:9/health", enabled=True, timeout_s=0.01,
                  max_attempts=4, backoff_base_s=0.25, backoff_cap_s=1.0)
    fields.update(overrides)
    return HealthPolicy(**fields)


class BackoffScheduleTest(unittest.TestCase):
    def test_exhaustion_sleeps_exponentially_and_is_capped(self):
        """base=0.25 cap=1.0, 4 attempts -> sleeps 0.25, 0.5, 1.0 (capped)."""
        clock = FakeClock()
        lines: list[str] = []
        gate = wait_for_healthy_server(
            _policy(), log=lines.append, sleep=clock,
            open_url=_fake_transport(None))
        self.assertEqual(clock.sleeps, [0.25, 0.5, 1.0])
        self.assertEqual(gate.outcome, HealthOutcome.UNHEALTHY)
        self.assertEqual(gate.attempts, 4)
        self.assertAlmostEqual(gate.total_wait_s, 1.75)
        self.assertEqual(gate.sleeps, (0.25, 0.5, 1.0))

    def test_delay_schedule_is_exponential_under_the_cap(self):
        policy = _policy(backoff_base_s=1.0, backoff_cap_s=4.0)
        self.assertEqual([backoff_delay_s(policy, n) for n in (1, 2, 3, 4, 5)],
                         [1.0, 2.0, 4.0, 4.0, 4.0])

    def test_recovery_mid_backoff_returns_healthy_with_only_past_sleeps(self):
        """Fail twice, succeed on the third probe: two sleeps were taken,
        the outcome is HEALTHY and the bound is not exhausted."""
        clock = FakeClock()
        gate = wait_for_healthy_server(
            _policy(), log=None, sleep=clock,
            open_url=_fake_transport(2))
        self.assertEqual(gate.outcome, HealthOutcome.HEALTHY)
        self.assertEqual(gate.attempts, 3)
        self.assertEqual(clock.sleeps, [0.25, 0.5])

    def test_immediate_success_never_sleeps(self):
        clock = FakeClock()
        lines: list[str] = []
        gate = wait_for_healthy_server(
            _policy(), log=lines.append, sleep=clock,
            open_url=_fake_transport(0))
        self.assertEqual(gate.outcome, HealthOutcome.HEALTHY)
        self.assertEqual(gate.attempts, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(lines, [], "a healthy first probe must stay quiet")

    def test_single_attempt_policy_never_sleeps_before_the_outcome(self):
        clock = FakeClock()
        gate = wait_for_healthy_server(
            _policy(max_attempts=1), sleep=clock,
            open_url=_fake_transport(None))
        self.assertEqual(gate.outcome, HealthOutcome.UNHEALTHY)
        self.assertEqual(gate.attempts, 1)
        self.assertEqual(clock.sleeps, [])


class LogLineTest(unittest.TestCase):
    """NFR-3: every backoff wait and the exhaustion are greppable."""

    def test_backoff_and_exhaustion_lines(self):
        lines: list[str] = []
        wait_for_healthy_server(_policy(), log=lines.append,
                                sleep=lambda s: None,
                                open_url=_fake_transport(None))
        joined = "\n".join(lines)
        self.assertEqual(joined.count("LLM-HEALTH-BACKOFF"), 3,
                         "one line per wait, none after the last probe")
        self.assertEqual(joined.count("LLM-HEALTH-EXHAUSTED"), 1)
        self.assertIn("url=http://fake:9/health", joined)

    def test_recovery_logs_the_wait_but_not_the_exhaustion(self):
        lines: list[str] = []
        wait_for_healthy_server(_policy(), log=lines.append,
                                sleep=lambda s: None,
                                open_url=_fake_transport(1))
        joined = "\n".join(lines)
        self.assertIn("LLM-HEALTH-BACKOFF", joined)
        self.assertNotIn("LLM-HEALTH-EXHAUSTED", joined)


class DisabledGateTest(unittest.TestCase):
    def test_disabled_gate_proceeds_without_probing_or_sleeping(self):
        clock = FakeClock()
        lines: list[str] = []
        transport = _fake_transport(None)
        gate = wait_for_healthy_server(_policy(enabled=False), log=lines.append,
                                       sleep=clock, open_url=transport)
        self.assertEqual(gate.outcome, HealthOutcome.DISABLED)
        self.assertEqual(gate, HealthGate(HealthOutcome.DISABLED, 0, 0.0))
        self.assertEqual(transport.state["probes"], 0)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(lines, [], "the disabled path must be silent (NFR-2)")


if __name__ == "__main__":
    unittest.main()
