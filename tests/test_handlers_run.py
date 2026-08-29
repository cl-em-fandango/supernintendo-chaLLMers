"""T67 — the run commands' claim handling, tested at the handler edge.

`run` and `run-one` are the two commands that turn a `pending/` directory into
claims, and each makes a promise the provider cannot make on its own. `cmd_run`
promises one bad task does not end the queue: the exception is caught inside
the loop, logged, and the next claim is fetched. `cmd_run_one` promises it will
work exactly one task and no more, whatever its claim fetch returned. Both
promise a claim they took and did not consume goes back to `pending/` on the way
out — including on the abort path, where the hand-back runs in `finally` — and
neither may touch a claim that is not its own: a peer invocation's claim, and a
claim with no readable owner at all, stay in `claimed/` with their ownership
intact, because `claimed/` is the input to the human review pass.

Covered here:
  * `cmd_run` continues past a raising task, reports the exception type and
    message, still returns 0, and keeps working one claim per fetch;
  * `cmd_run_one` processes exactly one of several pending tasks, claims the
    queue once, and does nothing at all on an empty queue;
  * every claim a run took is handed back under the owner id it was taken
    under — a normal cycle, a faulted cycle, an abort through the `finally`
    cleanup, and `run-one`'s unprocessed extras alike — with no sidecar left
    behind and no sweep of the whole `claimed/` directory;
  * a foreign claim and an ownerless claim survive both run paths in place,
    keep their recorded owner, and never appear in the run's release log.

Every fixture is a temp dir with `build()` patched, so the real queue is never
opened. The autonomous hand-off `cmd_run` reaches when the queue drains is made
inert: this file owns neither its behavior nor the stale-claim policy (the
stubbed cfg has no `.get()`, so the guard reads as off), nor the parser surface,
nor `cmd_status`.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core.claim_metadata import (  # noqa: E402
    OWNER_UNKNOWN,
    read_metadata,
    write_metadata,
)
from harness.core.providers import DirectoryTaskProvider, Task  # noqa: E402

# A claim that belongs to another invocation, planted before the run under test.
# Deliberately not an id either run command could generate.
FOREIGN_OWNER = "peer-invocation-9999"


class _SpyProvider(DirectoryTaskProvider):
    """The real provider, recording every claim-taking fetch and every requeue.

    The run commands' promises are about *which* owner each call named and
    *whether* the whole-directory sweep was reached, not only about where the
    files ended up, so the calls are captured as data rather than inferred from
    the directories afterwards.
    """

    def __init__(self, pending_dir, claimed_dir, log=print):
        super().__init__(pending_dir, claimed_dir, log=log)
        self.claims: list[dict] = []      # one entry per claim-taking fetch
        self.requeues: list[dict] = []    # one entry per requeue_claim call
        self.bulk_requeues = 0            # requeue_all_claims call count

    def fetch_pending(self, claim: bool = False, limit: int | None = None,
                      owner: str | None = None) -> list[Task]:
        tasks = super().fetch_pending(claim=claim, limit=limit, owner=owner)
        if claim:
            self.claims.append({"owner": owner, "limit": limit,
                                "ids": [t.id for t in tasks]})
        return tasks

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None,
                      force: bool = False) -> str | None:
        result = super().requeue_claim(name_or_task, owner=owner, force=force)
        name = (name_or_task.id if isinstance(name_or_task, Task)
                else str(name_or_task))
        self.requeues.append({"owner": owner, "task": name, "force": force,
                              "moved": result is not None})
        return result

    def requeue_all_claims(self, owner: str | None = None) -> list[str]:
        self.bulk_requeues += 1
        return super().requeue_all_claims(owner=owner)


class _StubPipeline:
    """A pipeline stub that never consumes a claim, and can fail on demand.

    The real pipeline's `release_claim` is what removes a claim; this stub does
    not, so every claim an invocation took is still held when its cleanup runs —
    which is the state the hand-back exists for. `faults` maps a task id to the
    exception raised in its place: a caught `Exception` exercises `cmd_run`'s
    skip path, a `BaseException` (an operator interrupt) exercises the
    `finally` hand-back. Fixture task names are chosen so the task id is the
    file stem, which is what makes a claim findable from the task alone.
    """

    lifecycle = None

    def __init__(self) -> None:
        self.processed: list[str] = []
        self.faults: dict[str, BaseException] = {}

    def process(self, task: Task) -> None:
        self.processed.append(task.id)
        fault = self.faults.get(task.id)
        if fault is not None:
            raise fault


class _NoopGenerator:
    """The autonomous hand-off made inert: `cmd_run` reaches it, never runs it."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs) -> int:
        return 0


class _RunFixture(unittest.TestCase):
    """A temp queue, the real provider, and stubbed `build()`/autonomous hand-off."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="t67-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = _SpyProvider(self.pending, self.claimed,
                                     log=self.messages.append)
        self.pipeline = _StubPipeline()

        # `build()` returns (cfg, store, runner, provider, pipeline, log) — the
        # 6-tuple read off composition.build() and the handlers' unpack, not a
        # copied arity. cfg only needs queue_dir/logs_dir: the stale-claim guard
        # reads its flag defensively, so a cfg without `.get()` means "off".
        cfg = types.SimpleNamespace(queue_dir=self.dir,
                                    logs_dir=self.dir / "logs")
        wired = (cfg, None, None, self.provider, self.pipeline,
                 lambda line="": self.messages.append(line))
        self._patch(handlers, "build", lambda *a, **k: wired)
        self._patch(handlers, "AutonomousGenerator", _NoopGenerator)

    def _patch(self, target, name: str, replacement) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.pending / name).write_text(f"# {name[:-3]}\nwork on {name}\n")

    def _plant_foreign_claim(self, name: str,
                             owner: str = FOREIGN_OWNER) -> Path:
        """A claim held by another invocation, sidecar and all."""
        claim_file = self.claimed / name
        claim_file.write_text(f"# {name[:-3]}\nnot this run's work\n")
        write_metadata(claim_file, owner)
        return claim_file

    def _plant_ownerless_claim(self, name: str) -> Path:
        """A claim taken before ownership existed: markdown, no sidecar."""
        claim_file = self.claimed / name
        claim_file.write_text(f"# {name[:-3]}\npre-ownership evidence\n")
        return claim_file

    def _pending_names(self) -> list[str]:
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def _sidecar_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.claim.json"))

    def _owner_of(self, name: str) -> str:
        return read_metadata(self.claimed / name).owner

    def _logged(self) -> str:
        return " | ".join(self.messages)

    def _claim_owners(self) -> list[str | None]:
        """The owner id of every claim-taking fetch this invocation performed."""
        return [entry["owner"] for entry in self.provider.claims]

    def _requeue_owners(self) -> list[str | None]:
        return [entry["owner"] for entry in self.provider.requeues]

    def _own_claim_owner(self) -> str:
        """The single owner id this invocation claimed under, asserted to exist."""
        owners = set(self._claim_owners())
        self.assertEqual(len(owners), 1,
                         f"one invocation used several owner ids: {owners}")
        owner = owners.pop()
        self.assertTrue(owner, f"the invocation claimed anonymously: {owner!r}")
        return owner


class RunLoopContinuationTest(_RunFixture):
    """`cmd_run`: one task raising must not strand the rest of the queue."""

    def test_a_raising_task_does_not_stop_the_rest_of_the_queue(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.pipeline.faults["001-a"] = RuntimeError("session died")
        self.assertEqual(handlers.cmd_run(), 0,
                         "a faulted task changed the run's exit code")
        self.assertEqual(self.pipeline.processed, ["001-a", "002-b", "003-c"],
                         "the loop stopped at the task that raised")

    def test_the_skipped_task_is_reported_with_its_exception(self):
        self._seed("001-a.md", "002-b.md")
        self.pipeline.faults["001-a"] = RuntimeError("session died")
        handlers.cmd_run()
        logged = self._logged()
        self.assertIn("task 001-a raised RuntimeError: session died", logged)
        self.assertIn("skipping", logged)
        self.assertIn("processing 002-b", logged,
                      "the queue was not worked past the faulted task")

    def test_a_later_task_can_raise_too_and_still_be_attempted(self):
        """The skip is per task, not one-shot: a second failure is survived too."""
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.pipeline.faults["001-a"] = RuntimeError("first")
        self.pipeline.faults["003-c"] = ValueError("second")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a", "002-b", "003-c"])
        self.assertIn("task 003-c raised ValueError: second", self._logged())

    def test_the_queue_is_worked_one_claim_per_fetch(self):
        """Continuation means a fresh single claim each cycle, not a bulk grab."""
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.pipeline.faults["002-b"] = ValueError("bad slice")
        handlers.cmd_run()
        self.assertEqual([entry["ids"] for entry in self.provider.claims],
                         [["001-a"], ["002-b"], ["003-c"]])
        self.assertEqual([entry["limit"] for entry in self.provider.claims],
                         [1, 1, 1],
                         "the loop claimed more than it was going to process")

    def test_a_run_over_an_empty_queue_reports_it_and_claims_nothing(self):
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertIn("pending queue empty", self._logged())
        self.assertEqual(self.provider.claims, [])
        self.assertEqual(self.pipeline.processed, [])


class RunOneProcessesOneTest(_RunFixture):
    """`cmd_run_one`: at most one task worked, whatever the fetch handed over."""

    def test_run_one_processes_exactly_one_of_several_pending(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run_one(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a"],
                         "run-one worked more than the one task it promised")

    def test_run_one_claims_the_queue_in_a_single_fetch(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        handlers.cmd_run_one()
        self.assertEqual([entry["ids"] for entry in self.provider.claims],
                         [["001-a", "002-b", "003-c"]],
                         "run-one claimed the queue more than once")

    def test_run_one_reports_the_claim_it_worked_and_the_one_it_returned(self):
        self._seed("001-a.md", "002-b.md")
        handlers.cmd_run_one()
        logged = self._logged()
        self.assertIn("processing 001-a (2 claimed this cycle)", logged)
        self.assertIn("requeued unprocessed claim: 002-b", logged)

    def test_run_one_on_an_empty_queue_claims_nothing_and_returns_zero(self):
        self.assertEqual(handlers.cmd_run_one(), 0)
        self.assertIn("no pending tasks", self._logged())
        self.assertEqual(self.provider.claims, [])
        self.assertEqual(self.provider.requeues, [])
        self.assertEqual(self.pipeline.processed, [])

    def test_run_one_with_a_single_claim_processes_it_and_requeues_nothing(self):
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run_one(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a"])
        self.assertEqual(self.provider.requeues, [],
                         "a cycle with no extras still tried a hand-back")


class OwnClaimReturnTest(_RunFixture):
    """Every claim a run took and did not consume goes back to `pending/`."""

    def test_a_full_run_hands_every_own_claim_back_to_pending(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self._pending_names(),
                         ["001-a.md", "002-b.md", "003-c.md"])
        self.assertEqual(self._claimed_names(), [],
                         "the run left its own claims sitting in claimed/")
        self.assertEqual(self._sidecar_names(), [],
                         "released claims left ownership sidecars behind")
        self.assertIn("released 3 unprocessed claim(s) back to pending",
                      self._logged())

    def test_a_claim_is_handed_back_under_the_owner_that_took_it(self):
        self._seed("001-a.md", "002-b.md")
        handlers.cmd_run()
        owner = self._own_claim_owner()
        self.assertEqual(self._requeue_owners(), [owner, owner],
                         "cleanup did not name the owner that took the claims")

    def test_a_faulted_tasks_claim_is_handed_back_too(self):
        """A skip is not a consumption: the claim it skipped on is still held."""
        self._seed("001-a.md", "002-b.md")
        self.pipeline.faults["001-a"] = RuntimeError("session died")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._sidecar_names(), [])

    def test_an_aborted_run_hands_back_the_claim_it_holds(self):
        """The hand-back is a `finally`: an interrupt must not strand its claim."""
        self._seed("001-a.md", "002-b.md")
        self.pipeline.faults["001-a"] = KeyboardInterrupt("operator interrupt")
        with self.assertRaises(KeyboardInterrupt):
            handlers.cmd_run()
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._claimed_names(), [],
                         "an aborted run stranded the claim it was holding")
        self.assertEqual(self._sidecar_names(), [])

    def test_run_one_hands_back_its_extras_under_the_owner_it_claimed_under(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run_one(), 0)
        owner = self._own_claim_owner()
        self.assertEqual(self._requeue_owners(), [owner, owner],
                         "run-one handed back claims under another id")
        self.assertEqual(self._pending_names(), ["002-b.md", "003-c.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._sidecar_names(), [])

    def test_no_run_path_sweeps_the_whole_claimed_directory(self):
        """A hand-back moves only its own claims; the bulk sweep is the operator's."""
        self._seed("001-a.md", "002-b.md")
        handlers.cmd_run()
        self._seed("003-c.md")
        handlers.cmd_run_one()
        self.assertEqual(self.provider.bulk_requeues, 0,
                         "a run path called requeue_all_claims")


class ForeignClaimRemainsTest(_RunFixture):
    """A claim this invocation did not take is not its to move."""

    def test_a_peer_claim_survives_a_full_run_in_place_and_owned(self):
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md", "002-b.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self._claimed_names(), ["099-peer.md"],
                         "a foreign claim was swept to pending/")
        self.assertEqual(self._owner_of("099-peer.md"), FOREIGN_OWNER,
                         "a foreign claim's ownership was rewritten")
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertNotIn(FOREIGN_OWNER, self._requeue_owners(),
                         "cleanup tried to move a claim it does not own")

    def test_a_peer_claim_survives_run_one(self):
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md", "002-b.md")
        self.assertEqual(handlers.cmd_run_one(), 0)
        self.assertEqual(self.pipeline.processed, ["001-a"])
        self.assertEqual(self._claimed_names(), ["099-peer.md"])
        self.assertEqual(self._owner_of("099-peer.md"), FOREIGN_OWNER)
        self.assertEqual(self._pending_names(), ["002-b.md"])
        self.assertNotIn(FOREIGN_OWNER, self._requeue_owners())

    def test_an_ownerless_claim_survives_both_run_paths(self):
        """No sidecar reads unknown; neither run command may hand it back."""
        self._plant_ownerless_claim("098-legacy.md")
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self._claimed_names(), ["098-legacy.md"])
        self.assertEqual(handlers.cmd_run_one(), 0)
        self.assertEqual(self._claimed_names(), ["098-legacy.md"])
        self.assertEqual(self._owner_of("098-legacy.md"), OWNER_UNKNOWN)
        self.assertEqual(self._pending_names(), ["001-a.md"])

    def test_a_foreign_claim_never_appears_in_the_run_s_release_log(self):
        """The release lines name only this run's own ids, so the log is honest."""
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md")
        handlers.cmd_run()
        logged = self._logged()
        self.assertIn("released claim: 001-a", logged)
        self.assertNotIn("099-peer", logged,
                         "a run reported releasing a claim it never touched")

    def test_a_foreign_claim_is_still_there_for_the_next_invocation(self):
        """Two runs over the same queue leave the peer's claim exactly as found."""
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md")
        handlers.cmd_run()
        self._seed("002-b.md")
        handlers.cmd_run()
        self.assertEqual(self._claimed_names(), ["099-peer.md"])
        self.assertEqual(self._owner_of("099-peer.md"), FOREIGN_OWNER)
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])


if __name__ == "__main__":
    unittest.main()
