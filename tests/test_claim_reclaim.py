"""T53 — stale reclaim and operator reclaim are ownership-aware.

T51 gave a claim a recorded owner and T52 made every run command claim and
clean up under one owner id, but both reclaim paths stayed owner-less: the
loop-start guard (`_requeue_stale_claims`) and the operator command
(`cmd_requeue_claims`) called `requeue_claim(claim)` with no owner — the
unchecked, pre-ownership call. Switching `--requeue-stale` on therefore handed
back a live peer's claim, and the operator command swept claims nobody could be
shown to hold, which is exactly what the ownership record exists to prevent.
Epic T46, leaf T53.

Covered here, against the real directory provider in temp dirs:
  * the provider's operator override — `requeue_claim(..., force=True)` moves a
    claim whatever the record says, while the default still refuses a foreign
    owner and an unnamed one;
  * the automatic sweep scoped to an owner: an old claim the named owner holds
    is reclaimed; an old claim held by another owner, one with no record and
    one with a corrupt record all stay claimed with their ownership intact,
    and every skip is logged;
  * age still gates both paths — a young owned claim is never stale, and force
    reaches no further than `older_than`;
  * the run commands scope the guard to their own owner id, so a full `cmd_run`
    / `cmd_run_task_loop` with the guard on leaves foreign, unknown and corrupt
    claims where they are;
  * the operator command names the recorded owner it hands back, refuses an
    unattributable claim until it is forced, prints that owner when it forces,
    and `--dry-run` still moves nothing.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.claim_metadata import OWNER_UNKNOWN  # noqa: E402
from harness.core.providers import (  # noqa: E402
    DirectoryTaskProvider,
    Task,
    TaskProvider,
)

# The owner id the automatic sweep is run under in the direct-call tests. A run
# generates its own per invocation (T52); here it is named so the tests can say
# which claims that owner holds.
OWNER = "run-1111-abcd"
PEER = "run-2222-9999"
STALE_HOURS = 48


class _QueueFixture(unittest.TestCase):
    """A temp pending/claimed pair and the shorthands the tests read back."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t53-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)

    def _plant(self, name: str, *, owner: str | None = None,
               hours_old: float | None = None,
               corrupt: bool = False) -> Path:
        """A claim already sitting in claimed/, owned and aged as described.

        No `owner` and no `corrupt` writes no record at all — a claim taken
        before ownership existed. `corrupt` writes a record that will not parse.
        Both read back as `OWNER_UNKNOWN`; the tests keep them apart because the
        operator has to be able to tell an orphan from damage.
        """
        claim_file = self.claimed / name
        claim_file.write_text(f"# {name[:-3]}\nbody of {name}\n")
        if corrupt:
            record = task_record.record_path(self.dir, name[:-3])
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text("{ not json")
        elif owner is not None:
            task_record.set_claim(self.dir, name[:-3], owner)
        if hours_old:
            stamp = time.time() - hours_old * 3600
            os.utime(claim_file, (stamp, stamp))
        return claim_file

    def _pending_names(self) -> list[str]:
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def _record_names(self) -> list[str]:
        meta = self.dir / task_record.META_DIR_NAME
        return sorted(p.name for p in meta.glob("*.json")) if meta.is_dir() \
            else []

    def _record_text(self, name: str) -> str:
        return task_record.record_path(self.dir, name[:-3]).read_text()

    def _owner_of(self, name: str) -> str:
        claim = task_record.read_record(self.dir, name[:-3]).claim
        return claim.owner if claim is not None else OWNER_UNKNOWN

    def _logged(self) -> str:
        return " | ".join(self.messages)


class ProviderForceTest(_QueueFixture):
    """`requeue_claim(..., force=True)` — the operator override at the provider."""

    def test_force_moves_a_claim_held_by_another_owner(self):
        self._plant("001-a.md", owner=PEER)
        dest = self.provider.requeue_claim("001-a.md", owner=OWNER, force=True)
        self.assertEqual(dest, str(self.pending / "001-a.md"))
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [],
                         "a forced requeue left an owner behind")

    def test_force_moves_a_claim_with_no_readable_owner(self):
        self._plant("002-b.md")
        self._plant("003-c.md", corrupt=True)
        self.assertIsNotNone(self.provider.requeue_claim("002-b.md", force=True))
        self.assertIsNotNone(self.provider.requeue_claim("003-c.md", owner=OWNER,
                                                         force=True))
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._pending_names(), ["002-b.md", "003-c.md"])

    def test_without_force_a_foreign_owner_is_still_refused(self):
        self._plant("001-a.md", owner=PEER)
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner=OWNER))
        self.assertIsNone(self.provider.requeue_claim("001-a.md",
                                                      owner=OWNER_UNKNOWN))
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._owner_of("001-a.md"), PEER)

    def test_force_on_a_claim_that_is_not_there_returns_none(self):
        self.assertIsNone(self.provider.requeue_claim("nope.md", force=True))
        self.assertIsNone(self.provider.requeue_claim(Task(id="nope", body=""),
                                                      owner=OWNER, force=True))
        self.assertEqual(self._pending_names(), [])

    def test_the_base_interface_takes_the_force_keyword(self):
        """A source with no claim lifecycle stays a valid adapter under force."""
        class NullProvider(TaskProvider):
            def fetch_pending(self) -> list[Task]:
                return []

        provider = NullProvider()
        self.assertIsNone(provider.requeue_claim(Task(id="x", body=""),
                                                 owner=OWNER, force=True))


class AutomaticStaleReclaimTest(_QueueFixture):
    """`_requeue_stale_claims(..., owner=...)` — how the run commands sweep."""

    def _sweep(self, owner: str | None = OWNER, enabled: bool = True) -> int:
        return handlers._requeue_stale_claims(self.provider, 6.0, enabled=enabled,
                                              log=self.messages.append, owner=owner)

    def test_an_old_claim_the_named_owner_holds_is_reclaimed(self):
        self._plant("001-a.md", owner=OWNER, hours_old=STALE_HOURS)
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [],
                         "the claim record outlived the claim it described")
        self.assertIn("reclaimed stale claim: 001-a (48h)", self._logged())

    def test_an_old_claim_held_by_another_owner_is_left_alone(self):
        self._plant("002-b.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self._pending_names(), [], "a foreign claim was stolen")
        self.assertEqual(self._owner_of("002-b.md"), PEER)
        logged = self._logged()
        self.assertIn("not reclaiming 002-b (48h)", logged)
        self.assertIn(PEER, logged, "the skip did not say who holds the claim")

    def test_a_claim_with_no_record_is_left_for_an_operator(self):
        self._plant("003-c.md", hours_old=STALE_HOURS)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(self._claimed_names(), ["003-c.md"])
        self.assertEqual(self._owner_of("003-c.md"), OWNER_UNKNOWN)
        logged = self._logged()
        self.assertIn("not reclaiming 003-c (48h)", logged)
        self.assertIn("no readable owner", logged)
        self.assertNotIn("--force", logged,
                         "the log named a CLI flag this leaf does not ship")

    def test_a_claim_with_a_corrupt_record_is_left_for_an_operator(self):
        self._plant("004-d.md", hours_old=STALE_HOURS, corrupt=True)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(self._claimed_names(), ["004-d.md"])
        self.assertEqual(self._record_text("004-d.md"), "{ not json",
                         "an automatic sweep rewrote the evidence")
        self.assertIn("not reclaiming 004-d (48h)", self._logged())

    def test_a_young_claim_of_the_named_owner_is_not_stale(self):
        self._plant("005-e.md", owner=OWNER)
        self.assertEqual(self._sweep(), 0)
        self.assertEqual(self._claimed_names(), ["005-e.md"])
        self.assertEqual(self._pending_names(), [])

    def test_a_disabled_guard_moves_nothing_even_when_the_claim_is_its_own(self):
        """The D4 rule survives ownership: off means off."""
        self._plant("001-a.md", owner=OWNER, hours_old=STALE_HOURS)
        self.assertEqual(self._sweep(enabled=False), 0)
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._logged(), "")

    def test_an_owner_less_sweep_is_the_pre_ownership_call(self):
        """No owner named = the operator's unchecked sweep, as T12 shipped it."""
        self._plant("001-a.md", owner=OWNER, hours_old=STALE_HOURS)
        self._plant("002-b.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("003-c.md", hours_old=STALE_HOURS)
        self._plant("006-f.md", owner=OWNER)
        self.assertEqual(self._sweep(owner=None), 3)
        self.assertEqual(self._claimed_names(), ["006-f.md"])
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md", "003-c.md"])

    def test_a_mixed_queue_moves_only_the_named_owner_s_stale_claim(self):
        self._plant("001-a.md", owner=OWNER, hours_old=STALE_HOURS)
        self._plant("002-b.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("003-c.md", hours_old=STALE_HOURS)
        self._plant("004-d.md", hours_old=STALE_HOURS, corrupt=True)
        self._plant("005-e.md", owner=OWNER)
        self.assertEqual(self._sweep(), 1)
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(),
                         ["002-b.md", "003-c.md", "004-d.md", "005-e.md"])
        self.assertEqual(self._record_names(),
                         ["002-b.json", "004-d.json", "005-e.json"],
                         "a claim that stayed held lost its owner")


class _SpyProvider(DirectoryTaskProvider):
    """The real provider, recording the owner each claim and requeue named."""

    def __init__(self, pending_dir, claimed_dir, log=print):
        super().__init__(pending_dir, claimed_dir, log=log)
        self.claim_owners: list[str | None] = []
        self.requeues: list[dict] = []

    def fetch_pending(self, claim: bool = False, limit: int | None = None,
                      owner: str | None = None) -> list[Task]:
        tasks = super().fetch_pending(claim=claim, limit=limit, owner=owner)
        if claim:
            self.claim_owners.append(owner)
        return tasks

    def requeue_claim(self, name_or_task: "str | Task",
                      owner: str | None = None,
                      force: bool = False) -> str | None:
        result = super().requeue_claim(name_or_task, owner=owner, force=force)
        self.requeues.append({"owner": owner, "force": force,
                              "moved": result is not None})
        return result


class _NoopPipeline:
    """A pipeline stub that never consumes a claim, so cleanup always sees it."""

    lifecycle = None

    def __init__(self):
        self.processed: list[str] = []

    def process(self, task: Task) -> None:
        self.processed.append(task.id)


class _NoopGenerator:
    """The autonomous hand-off made inert: `cmd_run` reaches it, never runs it."""

    def __init__(self, *args, **kwargs):
        pass

    def run(self, *args, **kwargs) -> int:
        return 0


class _WiredFixture(_QueueFixture):
    """The queue fixture plus the stubbed wiring the command paths need.

    `build()` returns the 6-tuple the handlers unpack, carrying the real
    provider; `_requeue_stale_claims` is wrapped, not replaced, so a command
    test sees both the owner id the guard was given and what it did with it.
    """

    def setUp(self):
        super().setUp()
        self.provider = _SpyProvider(self.pending, self.claimed,
                                     log=self.messages.append)
        self.pipeline = _NoopPipeline()
        cfg = types.SimpleNamespace(queue_dir=self.dir,
                                    logs_dir=self.dir / "logs")
        wired = (cfg, None, None, self.provider, self.pipeline,
                 lambda line="": self.messages.append(line))
        self._patch(handlers, "build", lambda *a, **k: wired)
        self._patch(handlers, "AutonomousGenerator", _NoopGenerator)
        self.guard_calls: list[dict] = []
        real_guard = handlers._requeue_stale_claims

        def record_guard(provider, older_hours, enabled, log=None, owner=None):
            self.guard_calls.append({"older_hours": older_hours,
                                     "enabled": enabled, "owner": owner})
            return real_guard(provider, older_hours, enabled, log=log, owner=owner)

        self._patch(handlers, "_requeue_stale_claims", record_guard)

    def _patch(self, target, name: str, replacement) -> None:
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.pending / name).write_text(f"# {name[:-3]}\nwork on {name}\n")


class RunPathStaleGuardTest(_WiredFixture):
    """The run commands scope their stale sweep to their own owner id."""

    def test_cmd_run_scopes_the_guard_to_its_own_owner_id(self):
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(requeue_stale=True), 0)
        self.assertEqual(len(self.guard_calls), 1)
        call = self.guard_calls[0]
        self.assertTrue(call["enabled"], "--requeue-stale did not reach the guard")
        self.assertEqual(call["older_hours"], handlers.CLAIM_STALE_HOURS)
        self.assertTrue(call["owner"], "the run swept stale claims anonymously")
        self.assertIn(call["owner"], self.provider.claim_owners,
                      "the guard was scoped to an id this run never claimed under")

    def test_cmd_run_task_loop_scopes_the_guard_to_its_own_owner_id(self):
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run_task_loop(requeue_stale=True), 0)
        call = self.guard_calls[0]
        self.assertTrue(call["enabled"])
        self.assertTrue(call["owner"])
        self.assertIn(call["owner"], self.provider.claim_owners)

    def test_a_run_with_the_guard_on_leaves_every_other_owner_alone(self):
        self._plant("099-peer.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("098-legacy.md", hours_old=STALE_HOURS)
        self._plant("097-corrupt.md", hours_old=STALE_HOURS, corrupt=True)
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run(requeue_stale=True), 0)
        self.assertEqual(self._claimed_names(),
                         ["097-corrupt.md", "098-legacy.md", "099-peer.md"])
        self.assertEqual(self._owner_of("099-peer.md"), PEER)
        self.assertEqual(self.pipeline.processed, ["001-a"])
        self.assertEqual(self._pending_names(), ["001-a.md"],
                         "the run did not hand back its own claim")

    def test_a_loop_with_the_guard_on_reclaims_nothing_it_does_not_own(self):
        """Nothing moves at all: the loop's own claim is held by the stub.

        `_NoopPipeline` never consumes a claim and the loop had no unprocessed
        extras to hand back, so `pending/` staying empty is the whole proof —
        no foreign, unknown or corrupt claim was turned into pending work.
        """
        self._plant("099-peer.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("098-legacy.md", hours_old=STALE_HOURS)
        self._seed("001-a.md")
        self.assertEqual(handlers.cmd_run_task_loop(requeue_stale=True), 0)
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self._owner_of("099-peer.md"), PEER)
        self.assertEqual(self._owner_of("098-legacy.md"), OWNER_UNKNOWN)
        self.assertEqual(self.pipeline.processed, ["001-a"])


class OperatorRequeueClaimsTest(_WiredFixture):
    """`cmd_requeue_claims` — names the owner it hands back, forces explicitly."""

    def _requeue(self, **kwargs) -> int:
        return handlers.cmd_requeue_claims(older_than=6.0, **kwargs)

    def test_a_requeue_names_the_recorded_owner_and_reports_it(self):
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(), 0)
        self.assertEqual(self.provider.requeues,
                         [{"owner": PEER, "force": False, "moved": True}],
                         "the operator requeue did not name the recorded owner")
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        logged = self._logged()
        self.assertIn(f"requeued 001-a (48h) owner={PEER}", logged)
        self.assertIn("requeued 1 of 1", logged)

    def test_an_unattributable_claim_is_refused_until_forced(self):
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self._plant("003-c.md", hours_old=STALE_HOURS, corrupt=True)
        self.assertEqual(self._requeue(), 0)
        self.assertEqual(self.provider.requeues, [],
                         "an unattributable claim was requeued without force")
        self.assertEqual(self._claimed_names(), ["002-b.md", "003-c.md"])
        self.assertEqual(self._pending_names(), [])
        logged = self._logged()
        self.assertIn("not requeueing 002-b (48h)", logged)
        self.assertIn("not requeueing 003-c (48h)", logged)
        self.assertIn("owner is unknown", logged)
        self.assertIn("requeued 0 of 2", logged)

    def test_force_moves_the_unattributable_claims_and_names_their_owner(self):
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self._plant("003-c.md", hours_old=STALE_HOURS, corrupt=True)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual(self.provider.requeues,
                         [{"owner": OWNER_UNKNOWN, "force": True, "moved": True},
                          {"owner": OWNER_UNKNOWN, "force": True, "moved": True}])
        self.assertEqual(self._pending_names(), ["002-b.md", "003-c.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [],
                         "a forced requeue left a record behind")
        logged = self._logged()
        self.assertIn(f"requeued 002-b (48h) owner={OWNER_UNKNOWN}", logged)
        self.assertIn(f"requeued 003-c (48h) owner={OWNER_UNKNOWN}", logged)
        self.assertIn("requeued 2 of 2", logged)

    def test_force_reaches_no_further_than_the_age_threshold(self):
        self._plant("004-d.md", hours_old=1)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual(self._claimed_names(), ["004-d.md"])
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])
        self.assertIn("requeued 0 of 0", self._logged())

    def test_dry_run_reports_owners_and_moves_nothing(self):
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(dry_run=True), 0)
        self.assertEqual(self._claimed_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])
        logged = self._logged()
        self.assertIn(f"would requeue 001-a (48h) owner={PEER}", logged)
        self.assertIn("not requeueing 002-b (48h)", logged)
        self.assertIn("dry run: 1 of 2 claim(s) at or over 6h "
                      "would move to pending/", logged)

    def test_dry_run_with_force_plans_the_unattributable_claim(self):
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(dry_run=True, force=True), 0)
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self._pending_names(), [])
        logged = self._logged()
        self.assertIn(f"would requeue 002-b (48h) owner={OWNER_UNKNOWN}", logged)
        self.assertIn("dry run: 1 of 1 claim(s) at or over 6h "
                      "would move to pending/", logged)

    def test_a_forced_requeue_still_never_overwrites_a_pending_name(self):
        (self.pending / "002-b.md").write_text("pending original")
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual((self.pending / "002-b.md").read_text(),
                         "pending original")
        self.assertEqual((self.pending / "002-b-requeued.md").read_text(),
                         "# 002-b\nbody of 002-b.md\n")

    def test_an_empty_claimed_directory_is_not_an_error(self):
        self.assertEqual(self._requeue(), 0)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual(self._requeue(dry_run=True), 0)
        self.assertEqual(self._pending_names(), [])
        self.assertIn("requeued 0 of 0", self._logged())


if __name__ == "__main__":
    unittest.main()
