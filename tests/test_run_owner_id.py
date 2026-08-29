"""T52 — one owner id per run-command invocation, used for claim and cleanup.

T51 gave the provider an ownership sidecar and made an ownership-checked requeue
refuse a foreign claim, but nothing generated an owner id: the run commands
claimed anonymously (`fetch_pending(claim=True)`) and cleaned up anonymously
(`requeue_claim(task)`), so the ownership gate was never reached from the CLI.
`harness/cli/handlers.py` now generates exactly one owner id per command
invocation (`_new_owner_id`) and passes it to every claim that invocation takes
and to the cleanup that hands its unprocessed claims back.

Covered here, against the real directory provider in temp dirs:
  * each invocation generates a distinct, non-empty owner id, and two
    invocations of the same command in one process still differ;
  * the claim a run holds is recorded against that run's own id (read from the
    sidecar while the run is inside `pipeline.process`);
  * the cleanup names the same id the claim was taken under — `run`,
    `run-one` and `run-task-loop` alike;
  * one invocation cannot clean another owner's claim: a peer's claim, and a
    pre-ownership claim with no sidecar, both survive a full run untouched;
  * a crashed run's claim (owner A) is still owner A's after a later invocation
    (owner B) runs over the same queue;
  * no run path sweeps the whole `claimed/` directory (`requeue_all_claims`).
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
    metadata_path,
    read_metadata,
    write_metadata,
)
from harness.core.providers import DirectoryTaskProvider, Task  # noqa: E402

# A claim that belongs to somebody else, planted before each invocation.
FOREIGN_OWNER = "peer-invocation-9999"


class _SpyProvider(DirectoryTaskProvider):
    """The real provider, with the owner id of every claim call recorded."""

    def __init__(self, pending_dir, claimed_dir, log=print):
        super().__init__(pending_dir, claimed_dir, log=log)
        self.claims: list[dict] = []      # one entry per claim-taking fetch
        self.requeues: list[dict] = []    # one entry per requeue_claim call
        self.bulk_requeues = 0            # requeue_all_claims call count

    def fetch_pending(self, claim: bool = False, limit: int | None = None,
                      owner: str | None = None) -> list[Task]:
        tasks = super().fetch_pending(claim=claim, limit=limit, owner=owner)
        if claim:
            self.claims.append({"owner": owner, "ids": [t.id for t in tasks]})
        return tasks

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None) -> str | None:
        result = super().requeue_claim(name_or_task, owner=owner)
        name = (name_or_task.id if isinstance(name_or_task, Task)
                else str(name_or_task))
        self.requeues.append({"owner": owner, "task": name,
                              "moved": result is not None})
        return result

    def requeue_all_claims(self, owner: str | None = None) -> list[str]:
        self.bulk_requeues += 1
        return super().requeue_all_claims(owner=owner)

    def held_owners(self) -> dict[str, str]:
        """`{claim filename: recorded owner}` for everything held in claimed/."""
        return {f.name: read_metadata(f).owner
                for f in sorted(self.claimed_dir.glob("*.md"))}


class _RecordingPipeline:
    """A pipeline stub that records the owner of the claim it is working on.

    It never consumes a claim (the real pipeline's `release_claim` does that),
    so every claim an invocation took is still held when its cleanup runs.
    Fixture task names are chosen so the task id is the file stem, which is
    what makes the claim file findable from the task alone.
    """

    lifecycle = None

    def __init__(self, claimed_dir: Path):
        self.claimed_dir = claimed_dir
        self.processed: list[str] = []
        self.owners_seen: dict[str, str] = {}
        self.raises: type[BaseException] | None = None

    def process(self, task: Task) -> None:
        self.processed.append(task.id)
        claim_file = self.claimed_dir / f"{task.id}.md"
        self.owners_seen[task.id] = (read_metadata(claim_file).owner
                                     if claim_file.exists() else OWNER_UNKNOWN)
        if self.raises is not None and len(self.processed) == 1:
            raise self.raises("simulated abort inside the session")


class _NoopGenerator:
    """The autonomous hand-off made inert: `cmd_run` reaches it, never runs it."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs) -> int:
        return 0


class _RunFixture(unittest.TestCase):
    """A temp queue, the real provider, and a stubbed `build()`/autonomous hand-off."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t52-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = _SpyProvider(self.pending, self.claimed,
                                     log=self.messages.append)
        self.pipeline = _RecordingPipeline(self.claimed)

        # `build()` returns (cfg, store, runner, provider, pipeline, log) — the
        # 6-tuple read off composition.build() and the handlers' unpack, not a
        # copied arity. cfg only needs queue_dir/logs_dir: the stale-claim guard
        # reads its flag defensively, so a cfg without `.get()` means "off".
        cfg = types.SimpleNamespace(queue_dir=self.dir, logs_dir=self.dir / "logs")
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

    def _plant_foreign_claim(self, name: str, owner: str = FOREIGN_OWNER) -> Path:
        """A claim held by another invocation, sidecar and all."""
        claim_file = self.claimed / name
        claim_file.write_text(f"# {name[:-3]}\nnot this run's work\n")
        write_metadata(claim_file, owner)
        return claim_file

    def _pending_names(self) -> list[str]:
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def _sidecar_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.claim.json"))

    def _claim_owners(self) -> list[str | None]:
        """The owner id of every claim-taking fetch this invocation performed."""
        return [entry["owner"] for entry in self.provider.claims]

    def _requeue_owners(self) -> list[str | None]:
        return [entry["owner"] for entry in self.provider.requeues]

    def _assert_foreign_untouched(self, name: str) -> None:
        self.assertIn(name, self._claimed_names(), "a foreign claim was swept")
        self.assertEqual(read_metadata(self.claimed / name).owner, FOREIGN_OWNER,
                         "a foreign claim's ownership was rewritten")


class OwnerIdGenerationTest(unittest.TestCase):
    """The id itself: one per invocation, unique, and readable in `claimed/`."""

    def test_ids_are_non_empty_and_named_for_their_command(self):
        for command in ("run", "run-one", "run-task-loop"):
            owner = handlers._new_owner_id(command)
            self.assertTrue(owner, f"{command} generated an empty owner id")
            self.assertTrue(owner.startswith(f"{command}-"),
                            f"{owner!r} does not name its command {command!r}")

    def test_two_invocations_of_one_command_never_share_an_id(self):
        ids = {handlers._new_owner_id("run") for _ in range(50)}
        self.assertEqual(len(ids), 50, "owner ids collided across invocations")

    def test_different_commands_get_different_ids(self):
        self.assertNotEqual(handlers._new_owner_id("run"),
                            handlers._new_owner_id("run-one"))

    def test_an_owner_id_is_a_usable_sidecar_owner(self):
        """Whatever the id is made of, it round-trips through the sidecar."""
        directory = Path(tempfile.mkdtemp(prefix="t52-id-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        claim_file = directory / "001-a.md"
        claim_file.write_text("A")
        owner = handlers._new_owner_id("run")
        write_metadata(claim_file, owner)
        self.assertEqual(read_metadata(claim_file).owner, owner)


class RunOwnershipTest(_RunFixture):
    """`cmd_run`: one id for its claims and for its finally-cleanup."""

    def test_each_invocation_claims_under_its_own_owner_id(self):
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(), 0)
        first = self._claim_owners()

        self.provider.claims.clear()
        self.assertEqual(handlers.cmd_run(), 0)
        second = self._claim_owners()

        for owners in (first, second):
            self.assertTrue(owners, "cmd_run claimed without taking a claim")
            self.assertTrue(all(owner for owner in owners),
                            f"cmd_run claimed anonymously: {owners}")
        self.assertNotEqual(set(first) & set(second), set(first),
                            "two invocations reused one owner id")

    def test_the_held_claim_records_the_running_invocation_s_id(self):
        self._seed("001-a.md")
        handlers.cmd_run()
        owner = self._claim_owners()[0]
        self.assertEqual(self.pipeline.owners_seen, {"001-a": owner},
                         "the claim was not recorded against this invocation")

    def test_cleanup_names_the_owner_the_claims_were_taken_under(self):
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run(), 0)
        owners = set(self._claim_owners())
        self.assertEqual(len(owners), 1, f"one run used several owner ids: {owners}")
        owner = owners.pop()
        self.assertEqual(self._requeue_owners(), [owner] * 3,
                         "cleanup did not name the owner that took the claims")
        self.assertEqual(self._claimed_names(), [], "the run left its own claims")
        self.assertEqual(self._pending_names(),
                         ["001-a.md", "002-b.md", "003-c.md"])
        self.assertEqual(self._sidecar_names(), [],
                         "released claims left ownership sidecars behind")

    def test_a_run_cannot_release_a_foreign_owner_s_claim(self):
        """The provider gate, driven with the ids the handlers generate."""
        self._seed("001-a.md")
        self.provider.fetch_pending(claim=True, owner="run-a-owns-it")
        task = self.provider.list_claims()[0]
        mine = handlers._new_owner_id("run")

        self.assertEqual(handlers._release_run_claims(self.provider, [task],
                                                      mine, self.messages.append),
                         0, "a foreign owner's claim was handed back")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._pending_names(), [])
        self.assertIn("not requeueing 001-a.md", " | ".join(self.messages))

        self.assertEqual(handlers._release_run_claims(self.provider, [task],
                                                      "run-a-owns-it",
                                                      self.messages.append), 1)
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])

    def test_a_peer_claim_survives_a_full_run(self):
        foreign = self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md", "002-b.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self._assert_foreign_untouched("099-peer.md")
        self.assertEqual(metadata_path(foreign).read_text().count(FOREIGN_OWNER), 1)
        self.assertNotIn(FOREIGN_OWNER, self._requeue_owners(),
                         "cleanup tried to move a claim it does not own")
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])

    def test_a_pre_ownership_claim_survives_a_full_run(self):
        """A claim with no sidecar reads unknown; an owned cleanup refuses it."""
        (self.claimed / "098-legacy.md").write_text("pre-ownership evidence\n")
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(), 0)
        self.assertEqual(self._claimed_names(), ["098-legacy.md"])
        self.assertEqual(read_metadata(self.claimed / "098-legacy.md").owner,
                         OWNER_UNKNOWN)

    def test_a_run_never_sweeps_the_whole_claimed_directory(self):
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md")
        handlers.cmd_run()
        self.assertEqual(self.provider.bulk_requeues, 0,
                         "a run path called requeue_all_claims")


class RunOneOwnershipTest(_RunFixture):
    """`cmd_run_one`: claims the queue under one id, hands back only its own."""

    def test_unprocessed_claims_return_under_the_run_s_own_id(self):
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run_one(), 0)

        owners = self._claim_owners()
        self.assertEqual(len(owners), 1, "run-one claimed more than once")
        owner = owners[0]
        self.assertTrue(owner)
        self.assertEqual(self.provider.claims[0]["ids"], ["001-a", "002-b", "003-c"])
        self.assertEqual(self._requeue_owners(), [owner, owner],
                         "run-one handed back claims under another id")
        self.assertEqual(self.pipeline.processed, ["001-a"])
        self.assertEqual(self._pending_names(), ["002-b.md", "003-c.md"])
        self._assert_foreign_untouched("099-peer.md")
        self.assertEqual(self.provider.bulk_requeues, 0)

    def test_a_second_run_one_invocation_has_a_different_id(self):
        self._seed("001-a.md")
        handlers.cmd_run_one()
        first = self._claim_owners()[0]
        self.provider.claims.clear()
        self._seed("002-b.md")
        handlers.cmd_run_one()
        second = self._claim_owners()[0]
        self.assertNotEqual(first, second)
        self.assertEqual(self._requeue_owners(), [],
                         "a single-claim cycle should requeue nothing")


class RunTaskLoopOwnershipTest(_RunFixture):
    """`cmd_run_task_loop`: one id across every cycle, and across a crash."""

    def test_every_cycle_claims_and_requeues_under_one_id(self):
        self._plant_foreign_claim("099-peer.md")
        self._seed("001-a.md", "002-b.md", "003-c.md")
        self.assertEqual(handlers.cmd_run_task_loop(), 0)

        claim_owners = set(self._claim_owners())
        requeue_owners = set(self._requeue_owners())
        self.assertEqual(len(claim_owners), 1,
                         f"one loop used several owner ids: {claim_owners}")
        owner = claim_owners.pop()
        self.assertTrue(owner)
        self.assertTrue(requeue_owners, "the loop claimed extras but requeued none")
        self.assertEqual(requeue_owners, {owner},
                         "the loop requeued under an id it never claimed with")
        self.assertEqual(self.pipeline.processed, ["001-a", "002-b", "003-c"])
        self.assertEqual(self._pending_names(), [])
        self._assert_foreign_untouched("099-peer.md")
        self.assertEqual(self.provider.bulk_requeues, 0)

    def test_a_crashed_run_s_claim_keeps_its_own_owner_after_a_later_run(self):
        """Owner A aborts mid-claim; owner B runs the same queue and cannot move it."""
        self._seed("001-a.md")
        self.pipeline.raises = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            handlers.cmd_run_task_loop()
        owner_a = self._claim_owners()[0]
        self.assertEqual(self.provider.held_owners(), {"001-a.md": owner_a},
                         "the crashed run's claim was not left recorded to it")

        self.provider.claims.clear()
        self.provider.requeues.clear()
        self.pipeline.raises = None
        self.assertEqual(handlers.cmd_run(), 0)

        owner_b = self._claim_owners()[0]
        self.assertNotEqual(owner_a, owner_b)
        self.assertEqual(self.provider.held_owners(), {"001-a.md": owner_a},
                         "a later invocation moved a claim it does not own")
        self.assertNotIn(owner_a, self._requeue_owners(),
                         "a later invocation tried to requeue a foreign claim")


if __name__ == "__main__":
    unittest.main()
