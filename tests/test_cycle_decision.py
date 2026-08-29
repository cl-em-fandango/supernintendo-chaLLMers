"""this test must never import `supervisor`.

`supervisor.py` runs `WORK_DIR = Path(load(CONFIG_PATH)...)` at import time, so
pulling it into a test reads the real `config.json` and creates real
directories — which is exactly why the cycle decision, the backoff math and the
breaker's counting live in the pure `harness.workflow.cycle` (T13). Everything
here goes through that module, or reads `supervisor.py` as *text* with `ast`:
the loop is never imported, never spawned, never forked, and no case in this
file writes a file outside a temp dir.

Run from the repo root:  python3 -m unittest tests.test_cycle_decision
"""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.workflow.cycle import (CycleAction,  # noqa: E402
                                    CycleSnapshot, backoff_seconds,
                                    command_for_action, cycle_summary,
                                    decide_cycle_action, made_progress,
                                    next_fail_state, subcommand_for_action)

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_SRC = (REPO_ROOT / "supervisor.py").read_text(encoding="utf-8")

# A stand-in interpreter path. `command_for_action` must put whatever it is
# given at the head of the argv, so a path that cannot be confused with
# `sys.executable` proves the value is threaded through and not hardcoded.
INTERPRETER = "/opt/test-env/bin/python3"

# T44's action: a claimed-only queue is blocked, not work.
BLOCKED = CycleAction.BLOCKED


# ---------------------------------------------------------------------------
# source-reading helpers (text only — nothing here imports `supervisor`)
# ---------------------------------------------------------------------------
def _call_name(call: ast.Call) -> str:
    """The bare callee name: `f(...)` -> `f`, `tracker.spawn(...)` -> `spawn`."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _run_loop() -> ast.FunctionDef:
    """The `run_loop()` node of `supervisor.py`, parsed but never executed."""
    for node in ast.walk(ast.parse(SUPERVISOR_SRC)):
        if isinstance(node, ast.FunctionDef) and node.name == "run_loop":
            return node
    raise AssertionError("supervisor.py no longer defines run_loop()")


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _strings(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _spawn_calls(loop: ast.FunctionDef) -> list[ast.Call]:
    """Every `*.spawn(...)` call in the loop — each one starts a child."""
    return [n for n in ast.walk(loop)
            if isinstance(n, ast.Call) and _call_name(n) == "spawn"]


def _argv_of(call: ast.Call) -> ast.expr:
    """The first argument of a `spawn()` call, `list(x)` unwrapped to `x`."""
    assert call.args, "a spawn() call was made without an argv"
    arg = call.args[0]
    if isinstance(arg, ast.Call) and _call_name(arg) == "list" and arg.args:
        arg = arg.args[0]
    return arg


class ImportPurityTest(unittest.TestCase):
    """The T13 import guard, promoted to CI (F11)."""

    def test_importing_the_pure_module_pulls_in_neither_supervisor_nor_config(self):
        """`harness.workflow.cycle` must stay importable on its own.

        Checked in a fresh interpreter: this process has already run other
        imports, so its own `sys.modules` would prove nothing.
        """
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.');"
             "import harness.workflow.cycle;"
             "assert 'supervisor' not in sys.modules, 'pulled in the supervisor';"
             "assert 'harness.core.config' not in sys.modules, 'pulled in config';"
             "print('clean')"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean", proc.stdout)

    def test_this_file_never_imports_the_supervisor_module(self):
        """The card's own first line, enforced on the card's own source.

        The forbidden token is built by concatenation so this file stays free
        of the exact string it is looking for.
        """
        src = Path(__file__).resolve().read_text(encoding="utf-8")
        self.assertNotIn("import " + "supervisor", src)


class DecideCycleActionTest(unittest.TestCase):
    """The table: in-flight beats pending beats claims beats generate (T13, T44)."""

    def test_in_flight_beats_pending(self):
        self.assertIs(decide_cycle_action(pending=5, in_flight=1, claims=0),
                      CycleAction.RESUME)

    def test_in_flight_beats_pending_and_claims(self):
        self.assertIs(decide_cycle_action(pending=5, in_flight=1, claims=2),
                      CycleAction.RESUME)

    def test_pending_produces_work(self):
        self.assertIs(decide_cycle_action(pending=3, in_flight=0, claims=0),
                      CycleAction.WORK)

    def test_claimed_only_is_blocked(self):
        """`pending=0, in_flight=0, claims>0` starts no child that could
        consume the claims: a state to name, not work to chase (T44)."""
        self.assertIs(decide_cycle_action(pending=0, in_flight=0, claims=2),
                      BLOCKED)
        self.assertIs(decide_cycle_action(pending=0, in_flight=0, claims=1),
                      BLOCKED)

    def test_pending_beats_claims(self):
        """One pending task is real work, and the claims wait for a later cycle."""
        self.assertIs(decide_cycle_action(pending=1, in_flight=0, claims=2),
                      CycleAction.WORK)

    def test_in_flight_beats_claims(self):
        self.assertIs(decide_cycle_action(pending=0, in_flight=1, claims=2),
                      CycleAction.RESUME)

    def test_all_zero_generates(self):
        self.assertIs(decide_cycle_action(pending=0, in_flight=0, claims=0),
                      CycleAction.GENERATE)

    def test_negative_counts_raise_value_error(self):
        """A negative count is a miscount, and a miscount must not decide."""
        for counts in ((-1, 0, 0), (0, -1, 0), (0, 0, -1)):
            with self.subTest(counts=counts):
                with self.assertRaises(ValueError):
                    decide_cycle_action(*counts)

    def test_large_counts_stay_sane(self):
        """No overflow, no float drift: the table is plain integer compares."""
        huge = 10 ** 12
        self.assertIs(decide_cycle_action(huge, huge, huge), CycleAction.RESUME)
        self.assertIs(decide_cycle_action(0, 1, huge), CycleAction.RESUME)
        self.assertIs(decide_cycle_action(huge, 0, 0), CycleAction.WORK)


class CycleSummaryTest(unittest.TestCase):
    """T14 logs this string verbatim, so its exact form is a contract."""

    def test_summary_is_the_exact_one_line_form(self):
        self.assertEqual(
            cycle_summary(1, 2, 3, CycleAction.RESUME),
            "pending=1 in_flight=2 claimed=3 action=resume")

    def test_summary_of_an_idle_queue(self):
        self.assertEqual(
            cycle_summary(0, 0, 0, CycleAction.GENERATE),
            "pending=0 in_flight=0 claimed=0 action=generate")

    def test_summary_of_a_claimed_only_queue(self):
        """T44: the blocked state is legible in the log line, count included."""
        self.assertEqual(
            cycle_summary(0, 0, 2, CycleAction.BLOCKED),
            "pending=0 in_flight=0 claimed=2 action=blocked")


class CommandForActionTest(unittest.TestCase):
    """T14's pure command mapping: one action, one argv, no literals in the loop."""

    def test_resume_maps_to_run_task_loop_continue(self):
        self.assertEqual(command_for_action(CycleAction.RESUME, INTERPRETER),
                         (INTERPRETER, "harness.py", "run-task-loop", "--continue"))

    def test_work_maps_to_run_task_loop_continue(self):
        self.assertEqual(command_for_action(CycleAction.WORK, INTERPRETER),
                         (INTERPRETER, "harness.py", "run-task-loop", "--continue"))

    def test_generate_maps_to_autonomous(self):
        self.assertEqual(command_for_action(CycleAction.GENERATE, INTERPRETER),
                         (INTERPRETER, "harness.py", "autonomous"))

    def test_blocked_maps_to_no_command_and_no_label(self):
        """T44: a claimed-only cycle spawns nothing, so it has no argv and no
        child-log label either."""
        self.assertEqual(command_for_action(BLOCKED, INTERPRETER), ())
        self.assertEqual(subcommand_for_action(BLOCKED), "")

    def test_the_log_label_is_a_subcommand_of_the_argv(self):
        """T08 names the child log after the subcommand, so the label must be
        the command that actually runs, never a drifted copy of it."""
        for action in (CycleAction.RESUME, CycleAction.WORK, CycleAction.GENERATE):
            with self.subTest(action=action):
                self.assertIn(subcommand_for_action(action),
                              command_for_action(action, INTERPRETER))
        self.assertEqual(subcommand_for_action(CycleAction.RESUME),
                         subcommand_for_action(CycleAction.WORK))


class SupervisorWiringTest(unittest.TestCase):
    """The decision-to-command seam, proven with `ast` — no import, no exec."""

    def test_run_loop_takes_its_child_argv_from_command_for_action(self):
        decided = {t.id
                   for stmt in ast.walk(_run_loop())
                   if isinstance(stmt, ast.Assign)
                   for t in stmt.targets
                   if isinstance(t, ast.Name)
                   and isinstance(stmt.value, ast.Call)
                   and _call_name(stmt.value) == "command_for_action"}
        self.assertTrue(decided,
                        "run_loop() never calls cycle.command_for_action()")
        sourced = [call for call in _spawn_calls(_run_loop())
                   if isinstance(_argv_of(call), ast.Name)
                   and _argv_of(call).id in decided]
        self.assertTrue(
            sourced,
            "no spawn() in run_loop() is given the command_for_action() result")

    def test_run_loop_holds_no_duplicated_command_literals(self):
        """The argv comes from `cycle.py`, so the subcommand names cannot drift
        into the loop as literals of their own."""
        strings = _strings(_run_loop())
        for literal in ("run-task-loop", "autonomous", "--continue"):
            self.assertNotIn(literal, strings,
                             f"run_loop() hardcodes the command literal {literal!r}")

    def test_the_only_literal_argv_is_the_status_probe(self):
        """The health probe may name its own command — it is not a decision.
        Anything else spawned from a literal is a second source of truth."""
        for call in _spawn_calls(_run_loop()):
            argv = _argv_of(call)
            if isinstance(argv, ast.List):
                self.assertEqual(_strings(argv), {"harness.py", "status"},
                                 "a decided child was spawned from a literal argv")


class SupervisorBreakerContractTest(unittest.TestCase):
    """The breaker's ordering, pinned as a contract on the loop's source.

    `next_fail_state` is the same arithmetic in pure form — the loop's copy sits
    between a spawn, a log and a `continue`, so it cannot be reached without
    running the loop. These cases pin that the loop still *orders* it the way
    the helper does: compare before reset, sleep on every failure path.
    """

    @staticmethod
    def _breaker_branch() -> ast.If:
        """The `if` block that counts launch failures."""
        for node in ast.walk(_run_loop()):
            if not isinstance(node, ast.If):
                continue
            counted = {n.target.id for n in ast.walk(node)
                       if isinstance(n, ast.AugAssign)
                       and isinstance(n.target, ast.Name)}
            if "failcount" in counted:
                return node
        raise AssertionError("run_loop() no longer counts launch failures in failcount")

    def test_fail_limit_is_compared_before_the_counter_is_reset(self):
        branch = self._breaker_branch()
        compares = [n.lineno for n in ast.walk(branch)
                    if isinstance(n, ast.Compare) and "FAIL_LIMIT" in _names(n)]
        self.assertTrue(compares, "the breaker never compares against FAIL_LIMIT")
        resets = [n.lineno for n in ast.walk(branch)
                  if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "failcount"
                          for t in n.targets)
                  and isinstance(n.value, ast.Constant) and n.value.value == 0]
        self.assertTrue(resets, "the breaker never resets failcount")
        self.assertLess(
            min(compares), min(resets),
            "failcount is cleared before FAIL_LIMIT is compared: a counter that "
            "resets itself first can never reach the limit, so the breaker "
            "could never fire")

    def test_every_failure_path_sleeps(self):
        """A failing cycle waits like any other, or it hammers a dead harness."""
        branch = self._breaker_branch()
        sleeps = [n.lineno for n in ast.walk(branch)
                  if isinstance(n, ast.Call) and _call_name(n) == "_sleep"]
        self.assertTrue(sleeps, "the breaker path never sleeps")
        resumes = [n.lineno for n in ast.walk(branch) if isinstance(n, ast.Continue)]
        self.assertTrue(resumes, "the breaker path never returns to the cycle head")
        for line in resumes:
            self.assertTrue(any(slept < line for slept in sleeps),
                            f"the cycle resumes at line {line} without sleeping")


class NextFailStateTest(unittest.TestCase):
    """The breaker's counting, in the pure form T38 extracted into `cycle.py`."""

    def test_a_successful_launch_resets_to_zero(self):
        state = next_fail_state(failcount=2, rc=0, limit=3)
        self.assertEqual(state.new_count, 0)
        self.assertFalse(state.should_reset)

    def test_a_failure_increments_below_the_limit(self):
        first = next_fail_state(failcount=0, rc=1, limit=3)
        self.assertEqual((first.new_count, first.should_reset), (1, False))
        second = next_fail_state(failcount=1, rc=127, limit=3)
        self.assertEqual((second.new_count, second.should_reset), (2, False))

    def test_the_increment_that_reaches_the_limit_trips(self):
        state = next_fail_state(failcount=2, rc=1, limit=3)
        self.assertTrue(state.should_reset)
        self.assertEqual(state.new_count, 0,
                         "a tripped breaker rolls back and starts counting again")

    def test_counting_restarts_after_a_reset(self):
        """The loop's whole `failcount` fold: three failures trip, one launch
        clears, three more trip again."""
        limit, failcount, trips = 3, 0, 0
        for rc in (1, 1, 1, 0, 1, 1, 1):
            failcount, should_reset = next_fail_state(failcount, rc, limit)
            trips += should_reset
        self.assertEqual(trips, 2)
        self.assertEqual(failcount, 0)

    def test_a_limit_of_one_trips_on_the_first_failure(self):
        state = next_fail_state(failcount=0, rc=1, limit=1)
        self.assertTrue(state.should_reset)
        self.assertEqual(state.new_count, 0)

    def test_bad_inputs_are_rejected(self):
        """A negative count or a zero limit makes the compare meaningless."""
        with self.assertRaises(ValueError):
            next_fail_state(failcount=-1, rc=1, limit=3)
        with self.assertRaises(ValueError):
            next_fail_state(failcount=0, rc=1, limit=0)


class CycleSnapshotIdentityTest(unittest.TestCase):
    """T47 (hardening review F6): progress is task identity, not three numbers.

    T15 compared `(pending, in_flight, claims)` before and after a child. A
    cycle that swaps one task for another keeps all three counts equal, so the
    count compare called real work a stall and backed off over it. `CycleSnapshot`
    holds the ids, `made_progress` compares them.
    """

    def test_equal_counts_with_changed_ids_is_progress(self):
        """The exact false stall T15 could not see, now progress."""
        before = CycleSnapshot(("a",), (), ())
        after = CycleSnapshot(("b",), (), ())
        self.assertEqual(before.counts, after.counts,
                         "the case must keep every count equal")
        self.assertTrue(made_progress(before, after))

    def test_identical_identities_are_not_progress(self):
        """The same tasks in the same places: an idle cycle, streak grows."""
        snapshot = CycleSnapshot(("a",), ("b",), ("c",))
        self.assertFalse(made_progress(snapshot, snapshot))
        self.assertFalse(made_progress(
            snapshot, CycleSnapshot(("a",), ("b",), ("c",))))

    def test_a_replacement_in_any_queue_is_progress(self):
        """Pending, in-flight and claimed ids all count."""
        base = CycleSnapshot(("a",), ("b",), ("c",))
        for other in (CycleSnapshot(("z",), ("b",), ("c",)),
                      CycleSnapshot(("a",), ("z",), ("c",)),
                      CycleSnapshot(("a",), ("b",), ("z",))):
            with self.subTest(other=other):
                self.assertEqual(base.counts, other.counts)
                self.assertTrue(made_progress(base, other))

    def test_a_task_moving_between_queues_is_progress(self):
        """One task leaving pending/ for active/ at equal totals."""
        before = CycleSnapshot(("a", "b"), (), ())
        after = CycleSnapshot(("a",), ("b",), ())
        self.assertEqual(before.counts.pending + before.counts.in_flight,
                         after.counts.pending + after.counts.in_flight)
        self.assertTrue(made_progress(before, after))

    def test_build_sorts_so_scan_order_is_not_progress(self):
        """The same tasks listed in another order are the same queue."""
        before = CycleSnapshot.build(["b", "a"], ["d", "c"], ["f", "e"])
        after = CycleSnapshot.build(["a", "b"], ["c", "d"], ["e", "f"])
        self.assertEqual(before, after)
        self.assertFalse(made_progress(before, after))
        self.assertEqual(before.pending, ("a", "b"))
        self.assertEqual(before.in_flight, ("c", "d"))
        self.assertEqual(before.claims, ("e", "f"))

    def test_counts_are_the_snapshot_lengths(self):
        """The logged numbers are derived from the ids, never scanned separately."""
        snapshot = CycleSnapshot.build(["a", "b"], ["c"], ["d", "e", "f"])
        self.assertEqual((snapshot.counts.pending, snapshot.counts.in_flight,
                          snapshot.counts.claims), (2, 1, 3))

    def test_an_empty_snapshot_is_all_empty(self):
        empty = CycleSnapshot((), (), ())
        self.assertEqual(empty.counts.pending, 0)
        self.assertFalse(made_progress(empty, CycleSnapshot.build([], [], [])))

    def test_a_snapshot_cannot_be_mutated(self):
        """Frozen: the before-snapshot cannot be edited into 'progress'."""
        snapshot = CycleSnapshot(("a",), (), ())
        with self.assertRaises(Exception):
            snapshot.pending = ("b",)  # type: ignore[misc]

    def test_unsorted_ids_are_rejected(self):
        """Unsorted ids would report progress from scan order alone."""
        with self.assertRaises(ValueError) as ctx:
            CycleSnapshot(("b", "a"), (), ())
        self.assertIn("pending", str(ctx.exception))
        with self.assertRaises(ValueError):
            CycleSnapshot((), ("d", "c"), ())
        with self.assertRaises(ValueError):
            CycleSnapshot((), (), ("f", "e"))


class SupervisorProgressContractTest(unittest.TestCase):
    """T47's seam in the loop's source, proven with `ast` — no import, no exec.

    The loop must decide and log from one snapshot's derived counts and ask
    `made_progress` whether the cycle moved anything: a second scan for the
    numbers, or a hand-written compare of the pair, is where the count-only
    stall would creep back in.
    """

    def test_run_loop_reads_its_counts_from_one_snapshot(self):
        calls = [n for n in ast.walk(_run_loop())
                 if isinstance(n, ast.Call)
                 and _call_name(n) in ("decide_cycle_action", "cycle_summary")]
        self.assertTrue(calls, "run_loop() no longer decides from the counts")
        for call in calls:
            for arg in call.args[:3]:
                self.assertIsInstance(
                    arg, ast.Attribute,
                    "a count passed to the decision is not a snapshot field")
                self.assertIsInstance(arg.value, ast.Name)
                self.assertEqual(
                    arg.value.id, "counts",
                    "a count was read from somewhere other than the "
                    "snapshot's derived `counts`")

    def test_run_loop_asks_made_progress_and_compares_nothing_itself(self):
        loop = _run_loop()
        self.assertTrue(
            any(isinstance(n, ast.Call) and _call_name(n) == "made_progress"
                for n in ast.walk(loop)),
            "run_loop() never calls cycle.made_progress()")
        for node in ast.walk(loop):
            if isinstance(node, ast.Compare) and \
                    any(isinstance(op, ast.NotEq) for op in node.ops):
                self.assertFalse({"before", "after"} & _names(node),
                                 "run_loop() compares the snapshots itself "
                                 "instead of asking made_progress()")

    def test_the_snapshot_is_built_from_ids_not_from_lengths(self):
        """`_queue_snapshot()` returns identities; counts come from the snapshot."""
        node = next((n for n in ast.walk(ast.parse(SUPERVISOR_SRC))
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "_queue_snapshot"), None)
        self.assertIsNotNone(node, "supervisor.py no longer defines _queue_snapshot()")
        self.assertTrue(
            any(isinstance(n, ast.Call) and _call_name(n) == "build"
                for n in ast.walk(node)),
            "_queue_snapshot() no longer builds a CycleSnapshot")
        self.assertNotIn("len", _names(node),
                         "_queue_snapshot() counts again instead of reading ids")


class BackoffShapeTest(unittest.TestCase):
    """T15's `backoff_seconds`, asserted for shape only (no duplicate added)."""

    def test_the_floor_is_the_base_sleep(self):
        """Streak 0 — before any idle cycle has piled up — is today's sleep."""
        self.assertEqual(backoff_seconds(0, 60, 900), 60)

    def test_is_monotonic_non_decreasing(self):
        sleeps = [backoff_seconds(n, 30, 600) for n in range(15)]
        self.assertEqual(sleeps, sorted(sleeps))

    def test_is_capped(self):
        for streak in range(30):
            self.assertLessEqual(backoff_seconds(streak, 30, 600), 600)
        self.assertEqual(backoff_seconds(29, 30, 600), 600,
                         "a long idle streak must flatten at the cap, not explode")


if __name__ == "__main__":
    unittest.main()
