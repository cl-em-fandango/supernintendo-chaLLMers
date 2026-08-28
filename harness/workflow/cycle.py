"""The pure cycle decision: in-flight beats claims beats pending beats generate (F1).

Three ints in, one `CycleAction` out. This module imports `dataclasses`, `enum`
and nothing else on purpose: `supervisor.py` loads the config and `WORK_DIR` at
import time, so importing it from a test reads real config and touches real
directories. Keeping the decision and the backoff math here, import-clean, is
what makes them testable. T14 wires the decision into the loop, T15 the
backoff.
"""
from dataclasses import dataclass
from enum import Enum


class CycleAction(str, Enum):
    """What one supervisor cycle does next."""

    RESUME = "resume"        # finish a task already in active/
    WORK = "work"            # work a task that is claimed or still pending
    GENERATE = "generate"    # nothing to do: ask for new tasks


def decide_cycle_action(pending: int, in_flight: int, claims: int) -> CycleAction:
    """Pick the cycle's action. First match wins:

    | condition        | action                 |
    |------------------|------------------------|
    | ``in_flight > 0``| ``CycleAction.RESUME`` |
    | ``claims > 0``   | ``CycleAction.WORK``   |
    | ``pending > 0``  | ``CycleAction.WORK``   |
    | otherwise        | ``CycleAction.GENERATE`` |

    A claim counts as *work*, not as garbage: ``cmd_run_task_loop`` requeues
    stale claims before the decision is made (T12), so whatever is left in
    ``claimed/`` is a task someone started and must finish. Treating it as
    nothing-to-do would hand started work back to generation and drop it.

    D4 caveat — a known blocked state, deliberately not handled here: T12's
    loop-start requeue ships off by default, so with the 7 live claims sitting
    put, ``claims > 0`` returns WORK forever and generation is blocked (T15's
    no-progress backoff bounds the cost). Two consequences: this function stays
    pure — three ints in, one action out, no policy, no thresholds — and it is
    the caller (T14) that decides which number to pass as ``claims``.

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
    if claims > 0:
        return CycleAction.WORK
    if pending > 0:
        return CycleAction.WORK
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


def cycle_summary(pending: int, in_flight: int, claims: int,
                  action: CycleAction) -> str:
    """The one-line form of a decision, for the log (T14 logs it verbatim)."""
    return (f"pending={pending} in_flight={in_flight} "
            f"claimed={claims} action={action.value}")


def command_for_action(action: CycleAction, python: str) -> tuple[str, ...]:
    """The child command for one cycle action, as an argv tuple.

    ``python`` is the interpreter the supervisor runs under, so the child uses
    the very same one (``sys.executable``). ``RESUME`` and ``WORK`` map to the
    same command: ``harness.py run-task-loop --continue`` resumes whatever is
    in ``active/`` first, then works the pending queue one claim at a time, so
    one child covers both. ``GENERATE`` maps to ``harness.py autonomous``.

    An action with no child returns an empty tuple and the caller spawns
    nothing — that is T44's ``BLOCKED`` slot, left empty here on purpose
    because this module must not invent an action before that card lands.

    The supervisor takes its argv from this function alone: command literals
    do not belong in ``run_loop()`` (T38 asserts the seam with ``ast``).
    """
    if action in (CycleAction.RESUME, CycleAction.WORK):
        return (python, "harness.py", "run-task-loop", "--continue")
    if action is CycleAction.GENERATE:
        return (python, "harness.py", "autonomous")
    return ()
