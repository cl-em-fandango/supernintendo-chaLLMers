"""T44 — a claimed-only queue blocks the cycle instead of generating no-op children.

`pending=0`, `in_flight=0`, `claims>0` is `CycleAction.BLOCKED`. The child both
RESUME and WORK spawn (`harness.py run-task-loop --continue`) resumes `active/`
and then drains `pending/`, and stale-claim reclaim is opt-in (decision D4), so
for a queue of nothing but claims every child it spawned exited without touching
a task. Such a cycle now spawns nothing at all, logs one operator-action line
naming `harness.py requeue-claims --dry-run` and the claim count, and idles
through the existing no-progress backoff (T15) — it never fails, moves or
requeues a claim, and stays responsive to SIGTERM and the STOP file.

Like `tests.test_supervisor_backoff`, this drives `run_loop()` with fakes in a
temp dir: the status probe is answered by a stub tracker, no `harness.py` child
is ever launched, and nothing is written outside the temp directory.
"""
from __future__ import annotations

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

CLAIMS = 3


class _FakeProvider:
    """A claimed-only queue: no pending work, `CLAIMS` claims, moves nothing.

    `requeue_claim` raises on purpose — the supervisor's job in a blocked cycle
    is to leave `claimed/` exactly as it found it, and a loop that tried would
    fail the run instead of the assertion.
    """

    def __init__(self, pending: int = 0, claims: int = CLAIMS):
        self.pending = pending
        self.claims = claims

    def fetch_pending(self, claim: bool = False,
                      limit: int | None = None) -> list[str]:
        assert claim is False, "a counting call must never claim the queue"
        return ["task"] * self.pending

    def list_claims(self) -> list[str]:
        return ["claim"] * self.claims

    def requeue_claim(self, task) -> None:
        raise AssertionError(
            f"a blocked cycle must not requeue a claim (got {task})")


class _RecordingTracker:
    """Every spawn is recorded; no child process is ever started."""

    def __init__(self, labels: list[str]):
        self.labels = labels

    def spawn(self, args, *, label: str) -> int:
        self.labels.append(label)
        return 0

    def kill_tree(self) -> None:
        pass


class BlockedCycleTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t44-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.provider = _FakeProvider()
        self.slept: list[int] = []
        self.labels: list[str] = []

        for patch in (
            mock.patch.object(S, "LOG", self.dir / "supervisor.log"),
            mock.patch.object(S, "STOPFILE", self.dir / "STOP"),
            mock.patch.object(S, "acquire_lock", lambda: True),
            mock.patch.object(S, "release_lock", lambda: None),
            mock.patch.object(S.signal, "signal", lambda *a, **k: None),
            mock.patch.object(S, "load", lambda path: mock.MagicMock()),
            mock.patch.object(S, "create_provider", lambda cfg: self.provider),
            mock.patch.object(S, "TaskLifecycle", lambda cfg, log=None: None),
            mock.patch.object(S, "in_flight_task_dirs", lambda lifecycle: []),
            mock.patch.object(S, "SLEEP_S", 60),
            mock.patch.object(S, "MAX_SLEEP_S", 900),
            mock.patch.object(S, "FAIL_LIMIT", 99),
            mock.patch.object(S, "ChildTracker",
                              lambda: _RecordingTracker(self.labels)),
            mock.patch.object(S, "_sleep", lambda stop, seconds:
                              self.slept.append(seconds)),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _run(self, cycles: int) -> str:
        """Run `cycles` supervised cycles over the claimed-only queue."""
        with mock.patch.object(S, "MAX_CYCLES", cycles):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(S.run_loop(), 0)
        return out.getvalue()

    def test_a_claimed_only_cycle_spawns_no_child(self):
        """The status probe is the only process; there is no work child at all."""
        self._run(3)
        self.assertEqual(self.labels, ["status"] * 3)

    def test_the_cycle_line_reads_blocked(self):
        """The state is visible in the log under the same word the code uses."""
        out = self._run(1)
        self.assertIn(f"pending=0 in_flight=0 claimed={CLAIMS} action=blocked",
                      out)

    def test_one_operator_line_naming_the_recovery_command_and_the_count(self):
        out = self._run(2)
        self.assertEqual(out.count("harness.py requeue-claims --dry-run"), 2,
                         "a blocked cycle logs exactly one operator-action line")
        self.assertIn(f"blocked: {CLAIMS} claim(s)", out)

    def test_a_blocked_cycle_backs_off_through_the_existing_path(self):
        """Nothing ran, so nothing moved: the sleep doubles like any idle cycle."""
        self._run(3)
        self.assertEqual(self.slept, [120, 240, 480])

    def test_a_blocked_cycle_stays_interruptible(self):
        """The backoff gets the loop's stop flag, so SIGTERM is honoured mid-wait."""
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

    def test_the_claims_are_left_exactly_where_they_are(self):
        """Blocking is a report, not a repair: no claim is moved or requeued."""
        self._run(2)
        self.assertEqual(len(self.provider.list_claims()), CLAIMS)

    def test_pending_work_is_still_work_while_claims_sit(self):
        """BLOCKED must not swallow a queue that has something to do."""
        self.provider = _FakeProvider(pending=1, claims=CLAIMS)
        out = self._run(1)
        self.assertEqual(self.labels, ["status", "run-task-loop"])
        self.assertIn("action=work", out)
        self.assertNotIn("requeue-claims --dry-run", out)


if __name__ == "__main__":
    unittest.main()
