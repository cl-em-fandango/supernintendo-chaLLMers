"""The pure cycle decision: in-flight, then pending, then blocked claims (F1, T44).

Three ints in, one `CycleAction` out. This module imports `dataclasses`, `enum`,
`typing` and nothing else on purpose: `supervisor.py` loads the config and
`WORK_DIR` at import time, so importing it from a test reads real config and
touches real directories. Keeping the decision, the backoff math and the circuit
breaker's counting here, import-clean, is what makes them testable. T14 wires
the decision into the loop, T15 the backoff.
"""
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class CycleAction(str, Enum):
    """What one supervisor cycle does next."""

    RESUME = "resume"        # finish a task already in active/
    WORK = "work"            # work a task that is still pending
    BLOCKED = "blocked"      # claims only: no child can consume them (T44)
    GENERATE = "generate"    # nothing to do: ask for new tasks


def decide_cycle_action(pending: int, in_flight: int, claims: int) -> CycleAction:
    """Pick the cycle's action. First match wins:

    | condition         | action                  |
    |-------------------|-------------------------|
    | ``in_flight > 0`` | ``CycleAction.RESUME``  |
    | ``pending > 0``   | ``CycleAction.WORK``    |
    | ``claims > 0``    | ``CycleAction.BLOCKED`` |
    | otherwise         | ``CycleAction.GENERATE``|

    A claim is not work (T44): the child both RESUME and WORK spawn —
    ``harness.py run-task-loop --continue`` — resumes ``active/`` and then
    drains ``pending/``, and neither step reads ``claimed/``. Automatic stale
    reclaim is opt-in (decision D4), so a queue of nothing but claims is a
    state the loop reaches by design and cannot leave on its own: calling it
    WORK bought an endless sequence of children that each exited without
    touching a task. ``BLOCKED`` names it for what it is — work an operator
    has to hand back with ``harness.py requeue-claims`` — and the caller logs
    that command and lets the no-progress backoff (T15) do the waiting.

    This function stays pure: three ints in, one action out, no policy, no
    thresholds, and no claim is moved, failed or requeued here.

    Raises:
        ValueError: if any count is negative. A negative count means the caller
            miscounted, and a miscount must not silently become a decision.
    """
    counts = (("pending", pending), ("in_flight", in_flight), ("claims", claims))
    for name, count in counts:
        if count < 0:
            raise ValueError(f"{name} must not be negative, got {count}")
    if in_flight > 0:
        return CycleAction.RESUME
    if pending > 0:
        return CycleAction.WORK
    if claims > 0:
        return CycleAction.BLOCKED
    return CycleAction.GENERATE


@dataclass(frozen=True)
class QueueCounts:
    """A read-only snapshot of the three queue directories.

    It exists so the loop's progress test has a name and a shape instead of a
    loose 3-tuple: two snapshots compare by value, so `after != before` is the
    whole test — any state change (claim→active, active→done/parked, new
    pending from generation) moves at least one number, and a cycle that moved
    nothing is a cycle that accomplished nothing.
    """

    pending: int
    in_flight: int
    claims: int


def backoff_seconds(idle_streak: int, base: int, cap: int) -> int:
    """How long to sleep after `idle_streak` consecutive no-progress cycles.

    `min(base * 2 ** idle_streak, cap)`: streak 0 returns `base` — today's
    behavior, so a healthy loop is untouched — and every further idle cycle
    doubles the sleep until it flattens at `cap`. A wedged task or an
    unreachable model endpoint therefore costs a shrinking share of the CPU and
    log volume instead of a full probe-and-spawn every `base` seconds forever.

    Args:
        idle_streak: consecutive cycles that changed nothing; must be >= 0.
        base: the normal sleep (the supervisor's `SLEEP_S`), returned at streak 0.
        cap: the longest the loop may ever idle (its `MAX_SLEEP_S`).

    Raises:
        ValueError: if `idle_streak` is negative — a negative streak means the
            caller miscounted, and a miscount must not silently pick a duration.
    """
    if idle_streak < 0:
        raise ValueError(f"idle_streak must not be negative, got {idle_streak}")
    return min(base * 2 ** idle_streak, cap)


class FailState(NamedTuple):
    """The circuit breaker's counter after one cycle.

    `new_count` is what the loop stores back into `failcount`; `should_reset`
    says this cycle tripped the breaker, so the loop rolls trunk back to
    `pi/last-good`. A NamedTuple because the pair is returned, not passed
    around: it destructures as `(new_count, should_reset)` and still names both
    fields (CODING_STANDARDS §2 — no bare tuple for meaningful state).
    """

    new_count: int
    should_reset: bool


def next_fail_state(failcount: int, rc: int, limit: int) -> FailState:
    """The launch-failure counter after one cycle, and whether it tripped.

    The supervisor's breaker counts consecutive cycles in which `harness.py
    status` could not be launched at all: a non-zero `rc` increments, a zero
    `rc` clears the count, and the increment that *reaches* `limit` is the one
    that trips — the breaker reverts trunk and starts counting again from 0.
    The comparison therefore happens before the reset, and the reset belongs to
    the tripping increment alone: a counter that reset on every cycle could
    never reach the limit, and one that compared after the reset could never
    fire at all.

    Extracted from `run_loop()` (T38) because that arithmetic sits between a
    spawn, a log and a `continue` and cannot be reached without running the
    loop; the loop keeps its own copy of the sequence, and
    `tests/test_cycle_decision.py` pins the sequence with `ast`.

    Args:
        failcount: failures counted so far; must be >= 0.
        rc: the status probe's exit code. 0 means the harness launched.
        limit: failures in a row that trip the breaker (`FAIL_LIMIT`); >= 1.

    Raises:
        ValueError: if `failcount` is negative or `limit` is below 1 — either
            one makes the comparison meaningless, and a miscount must not
            silently decide whether trunk rolls back.
    """
    if failcount < 0:
        raise ValueError(f"failcount must not be negative, got {failcount}")
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    if rc == 0:
        return FailState(new_count=0, should_reset=False)
    count = failcount + 1
    if count >= limit:
        return FailState(new_count=0, should_reset=True)
    return FailState(new_count=count, should_reset=False)


def cycle_summary(pending: int, in_flight: int, claims: int,
                  action: CycleAction) -> str:
    """The one-line form of a decision, for the log (T14 logs it verbatim).

    The action is rendered from the enum, so a claimed-only queue reads
    ``pending=0 in_flight=0 claimed=2 action=blocked`` (T44): the state is
    visible in the log under the same word the code uses for it.
    """
    return (f"pending={pending} in_flight={in_flight} "
            f"claimed={claims} action={action.value}")


def subcommand_for_action(action: CycleAction) -> str:
    """The ``harness.py`` subcommand an action runs, ``""`` when it runs none.

    The name of the subcommand, without the interpreter or the flags. T08's
    child log uses it as the log label, so ``logs/children/<ts>-<label>.log``
    is named after the command a human would rerun by hand (``run-task-loop``,
    ``autonomous``) rather than after the internal action that picked it —
    ``RESUME`` and ``WORK`` share one subcommand and therefore one label.
    ``BLOCKED`` runs no child and therefore has no label.

    ``command_for_action`` builds its argv from this value, so the label can
    never drift from the command it labels.
    """
    if action in (CycleAction.RESUME, CycleAction.WORK):
        return "run-task-loop"
    if action is CycleAction.GENERATE:
        return "autonomous"
    return ""


def command_for_action(action: CycleAction, python: str) -> tuple[str, ...]:
    """The child command for one cycle action, as an argv tuple.

    ``python`` is the interpreter the supervisor runs under, so the child uses
    the very same one (``sys.executable``). ``RESUME`` and ``WORK`` map to the
    same command: ``harness.py run-task-loop --continue`` resumes whatever is
    in ``active/`` first, then works the pending queue one claim at a time, so
    one child covers both. ``GENERATE`` maps to ``harness.py autonomous``.

    An action with no child returns an empty tuple and the caller spawns
    nothing — that is ``CycleAction.BLOCKED`` (T44): a claimed-only queue has
    no command that could consume it, so the cycle runs no child at all.

    The supervisor takes its argv from this function alone: command literals
    do not belong in ``run_loop()`` (T38 asserts the seam with ``ast``).
    """
    subcommand = subcommand_for_action(action)
    if not subcommand:
        return ()
    if action is CycleAction.GENERATE:
        return (python, "harness.py", subcommand)
    return (python, "harness.py", subcommand, "--continue")
