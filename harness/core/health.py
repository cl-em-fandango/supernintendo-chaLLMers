"""LLM model-server health pre-flight (FR-5.1).

Before the harness dispatches a session to the model server it asks one
cheap question: is the server answering HTTP at all? A dispatch against a
dead server burns a crash-retry attempt (FR-5.4) for a reason the harness
could have waited out, so the pre-flight waits instead — bounded
exponential backoff, then a distinct "server unhealthy" outcome that is
not a session crash.

State and behavior are split in the module's own two sections: the
dataclasses (`HealthPolicy`, `HealthGate`) describe the shape; the
functions (`probe_llm_health`, `wait_for_healthy_server`) act on them.
The probe is stdlib `urllib` only and the transport (`open_url`) and the
clock (`sleep`) are injectable, so tests run it in-process against fake
responders and closed ports — no live model server required (NFR-4).

Disabled-safe: with no endpoint configured the gate returns immediately
and touches neither network nor clock, preserving today's behavior
byte-for-byte (NFR-2).

Log lines are greppable per NFR-3: `LLM-HEALTH-BACKOFF` on each wait,
`LLM-HEALTH-EXHAUSTED` when the bound runs out. A healthy probe and a
disabled gate log nothing, so the normal path stays quiet.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Callable

# Defaults for the FR-5.1 knobs (config keys in harness/core/config.py).
DEFAULT_HEALTH_TIMEOUT_S = 5.0
DEFAULT_HEALTH_MAX_ATTEMPTS = 5
DEFAULT_HEALTH_BACKOFF_BASE_S = 2.0
DEFAULT_HEALTH_BACKOFF_CAP_S = 30.0


class HealthOutcome(Enum):
    """The pre-flight's discrete verdict for one dispatch gate."""

    DISABLED = "disabled"    # no endpoint configured — proceed unconditionally
    HEALTHY = "healthy"      # the server answered; dispatch may proceed
    UNHEALTHY = "unhealthy"  # backoff exhausted; the server never answered


@dataclass
class HealthPolicy:
    """The FR-5.1 knobs as one explicit parameters object."""

    url: str
    enabled: bool
    timeout_s: float
    max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float


@dataclass
class HealthGate:
    """The result of one pre-flight: outcome plus what it cost to get there.

    `sleeps` records every backoff delay actually taken (injectable clock),
    so a test can observe the backoff schedule without wall-clock time.
    """

    outcome: HealthOutcome
    attempts: int
    total_wait_s: float
    sleeps: tuple[float, ...] = ()


def probe_llm_health(policy: HealthPolicy, *,
                     open_url: Callable | None = None) -> bool:
    """One probe: True when the model server is answering HTTP.

    Any HTTP response with a status below 500 means the server process is
    up and serving — even a 404 from a path the server does not route says
    the listener is alive, which is all the pre-flight needs. A 5xx, a
    transport failure (connection refused, DNS, timeout) or any socket
    error means unhealthy. Never raises: the probe's contract is a bool.
    """
    opener = open_url or urllib.request.urlopen
    request = urllib.request.Request(policy.url, method="GET")
    try:
        with opener(request, timeout=policy.timeout_s) as response:
            return int(getattr(response, "status", 200)) < 500
    except urllib.error.HTTPError as exc:
        # The server answered with an error status. Below 500 the process
        # is alive (e.g. the health path is simply not routed); 5xx is the
        # server telling us it cannot serve.
        return exc.code < 500
    except Exception:
        # URLError, socket timeouts, connection refused — nothing answered.
        return False


def backoff_delay_s(policy: HealthPolicy, attempt: int) -> float:
    """The sleep after failed `attempt` (1-based): base * 2^(attempt-1), capped."""
    return min(policy.backoff_base_s * (2 ** (attempt - 1)),
               policy.backoff_cap_s)


def wait_for_healthy_server(policy: HealthPolicy, *,
                            log: Callable[[str], None] | None = None,
                            sleep: Callable[[float], None] = time.sleep,
                            open_url: Callable | None = None) -> HealthGate:
    """Gate one dispatch: probe, and on failure sleep with exponential
    backoff, up to `max_attempts` probes.

    Returns as soon as a probe succeeds (HEALTHY) or immediately when the
    gate is disabled (DISABLED — no probe, no sleep). When the attempts are
    exhausted the outcome is UNHEALTHY — a distinct result the caller
    routes on instead of spending a crash-retry attempt (FR-5.4).
    """
    if not policy.enabled:
        return HealthGate(HealthOutcome.DISABLED, attempts=0, total_wait_s=0.0)
    sleeps: list[float] = []
    total_wait = 0.0
    for attempt in range(1, policy.max_attempts + 1):
        if probe_llm_health(policy, open_url=open_url):
            return HealthGate(HealthOutcome.HEALTHY, attempts=attempt,
                              total_wait_s=total_wait, sleeps=tuple(sleeps))
        if attempt == policy.max_attempts:
            break
        delay = backoff_delay_s(policy, attempt)
        if log is not None:
            log(f"  LLM-HEALTH-BACKOFF attempt={attempt}/{policy.max_attempts} "
                f"url={policy.url} server unhealthy; sleeping {delay:.1f}s")
        sleep(delay)
        sleeps.append(delay)
        total_wait += delay
    if log is not None:
        log(f"  LLM-HEALTH-EXHAUSTED attempts={policy.max_attempts} "
            f"url={policy.url} waited={total_wait:.1f}s — server unhealthy")
    return HealthGate(HealthOutcome.UNHEALTHY, attempts=policy.max_attempts,
                      total_wait_s=total_wait, sleeps=tuple(sleeps))
