"""T66 — the claim-facing half of `cli/handlers.py`, tested at the handler edge.

`claimed/` is where the F2 leak hid, and it is the one queue directory that
holds work a run owns rather than work waiting: read it wrong and an operator
cannot see the pile, clear it wrong and a live peer's claim becomes pending
twice. The two commands that read and clear it are pinned here rather than at
the provider — `cmd_status`, the only inspection tool the repo has, and
`cmd_requeue_claims`, the operator's recovery path. The provider's own claim
mechanics are T09's, the ownership record is T51's, and the automatic
stale sweep plus the run commands' owner scoping are T53's. What this file owns
is the handler surface:

  * the `claimed` status row — lifecycle row order, the `<id> (<age>h)` labels,
    `-` when nothing is held, the stranded warning with its count, a claim the
    provider cannot age listed without an age, and ownership records never
    becoming claims of their own;
  * `--dry-run` — the plan is printed with each claim's recorded owner, the
    refused ones are named as refused, and nothing is handed to the provider;
  * an empty `claimed/` — the healthy case: 0, no raise, no provider call, for
    every flag combination, and the same zero when claims exist but none is old
    enough to select;
  * `force` — refused without it, honored with it, names the owner it overrode,
    reaches no further than `older_than`, and never overwrites a pending name;
  * id↔filename matching — a claim whose `_slug`-ified task id is not its
    filename (dots, and a name truncated at the 60-char slug limit) is still
    aged, still listed, and still handed back under its original filename.

Every fixture is a temp dir with `build()` patched, so the real work tree is
never opened. Aged claims get one second of margin beyond the requested age so
a filesystem that rounds an mtime cannot push a whole-hour label down an hour.
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
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402
from harness.workflow.task_lifecycle import (  # noqa: E402
    CLAIMED_LOCATION,
    QUEUE_LOCATIONS_ALL,
)

# The two owners the fixtures attribute claims to. Neither is a run id these
# tests generate: the handler under test acts on an operator's authority and
# names whatever owner the record holds, so the ids only have to be
# distinguishable from each other and from `OWNER_UNKNOWN`.
OWNER = "run-1111-abcd"
PEER = "run-2222-9999"

STALE_HOURS = 48          # comfortably over the threshold below
YOUNG_HOURS = 1           # somebody's live work: no flag may move it
THRESHOLD = 6.0           # the `--older-than` every reclaim test passes


def _row(location: str, count: int, items: str) -> str:
    """One status row exactly as `cmd_status` renders it."""
    return f"{location:<10} ({count}): {items}"


class _SpyProvider(DirectoryTaskProvider):
    """The real provider, recording every `requeue_claim` the handler made.

    The handler's promises are about *which* owner and *whether* force reached
    the provider, not only about where the files ended up, so the calls are
    captured as data rather than inferred from the directory afterwards.
    """

    def __init__(self, pending_dir, claimed_dir, log=print):
        super().__init__(pending_dir, claimed_dir, log=log)
        self.requeues: list[dict] = []

    def requeue_claim(self, name_or_task, owner=None, force=False):
        result = super().requeue_claim(name_or_task, owner=owner, force=force)
        self.requeues.append({"owner": owner, "force": force,
                              "moved": result is not None})
        return result


class _WiredFixture(unittest.TestCase):
    """A temp queue, the real directory provider, and `build()` pointed at both.

    The tuple is the 6-tuple `composition.build()` returns today — cfg, store,
    runner, provider, pipeline, log — read off the code, not out of a card. The
    store is a real `StatsStore` over a temp file so `cmd_status` renders its
    report for real; runner and pipeline are `None` because neither claim
    handler touches either.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t66-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = _SpyProvider(self.pending, self.claimed,
                                     log=self.messages.append)
        cfg = types.SimpleNamespace(work_dir=self.dir,
                                    queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, self.provider, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plant(self, name: str, *, owner: str | None = None,
               hours_old: float | None = None,
               corrupt: bool = False) -> Path:
        """A claim already sitting in `claimed/`, owned and aged as described.

        No `owner` and no `corrupt` writes no record at all — a claim taken
        before ownership existed. `corrupt` writes one that will not parse. Both
        read back as `OWNER_UNKNOWN`; the fixtures keep them apart because an
        operator has to tell an orphan from damage. Returns the claim path so a
        test can read the body it planted rather than retype it.
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
            stamp = time.time() - (hours_old * 3600) - 1
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

    def _legacy_sidecar_names(self) -> list[str]:
        return sorted(p.name for p in self.claimed.glob("*.claim.json"))

    def _owner_of(self, name: str) -> str:
        claim = task_record.read_record(self.dir, name[:-3]).claim
        return claim.owner if claim is not None else OWNER_UNKNOWN

    def _logged(self) -> str:
        return " | ".join(self.messages)

    def _status_rows(self) -> list[str]:
        """The queue rows `cmd_status` logged, before the warning and report."""
        self.assertEqual(handlers.cmd_status(), 0)
        return self.messages[:len(QUEUE_LOCATIONS_ALL)]


class StatusClaimRowTest(_WiredFixture):
    """The `claimed` row: what it lists, how it ages it, what it warns about."""

    def test_the_claimed_row_lists_every_claim_with_its_age(self):
        self._plant("009-stuck.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("010-later.md", owner=PEER, hours_old=STALE_HOURS + 3)
        self.assertIn(_row(CLAIMED_LOCATION, 2,
                           "009-stuck (48h), 010-later (51h)"), self._status_rows())

    def test_row_order_is_lifecycle_shaped_with_claimed_second(self):
        names = [row.split()[0] for row in self._status_rows()]
        self.assertEqual(names, list(QUEUE_LOCATIONS_ALL))
        self.assertEqual(names[1], CLAIMED_LOCATION)

    def test_an_empty_claimed_row_reads_as_a_dash_and_warns_nothing(self):
        self.assertIn(_row(CLAIMED_LOCATION, 0, "-"), self._status_rows())
        self.assertNotIn("claimed tasks", self._logged())

    def test_the_warning_names_the_count_and_the_command_that_clears_it(self):
        self._plant("009-stuck.md", owner=PEER)
        self.assertEqual(handlers.cmd_status(), 0)
        logged = self._logged()
        self.assertIn("⚠ 1 claimed tasks", logged)
        self.assertIn("requeue-claims", logged)
        self.assertNotIn("T12", logged,
                         "the warning still names a plan card, not the command")

    def test_a_claim_the_provider_cannot_age_is_listed_without_an_age(self):
        """-1.0 means "no age known", and a bogus `( -1h)` is not a substitute."""
        self._plant("009-stuck.md", owner=PEER, hours_old=STALE_HOURS)
        with mock.patch.object(self.provider, "claim_age_hours",
                               return_value=-1.0):
            rows = self._status_rows()
        self.assertIn(_row(CLAIMED_LOCATION, 1, "009-stuck"), rows)
        self.assertNotIn("(48h)", self._logged())
        self.assertNotIn("-1h", self._logged())

    def test_ownership_records_are_never_listed_as_claims(self):
        self._plant("009-stuck.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._record_names(), ["009-stuck.json"])
        self.assertEqual(self._legacy_sidecar_names(), [])
        self.assertIn(_row(CLAIMED_LOCATION, 1, "009-stuck (48h)"),
                      self._status_rows())
        self.assertNotIn(".json", self._logged())

    def test_the_claimed_row_counts_claims_not_the_other_queue_directories(self):
        """A pending file is pending work, not a claim, even with the same name."""
        (self.pending / "009-stuck.md").write_text("waiting work")
        self._plant("009-stuck.md", owner=PEER, hours_old=STALE_HOURS)
        rows = self._status_rows()
        self.assertIn(_row(CLAIMED_LOCATION, 1, "009-stuck (48h)"), rows)
        self.assertIn(_row("pending", 1, "009-stuck.md"), rows)


class DryRunTest(_WiredFixture):
    """`cmd_requeue_claims(dry_run=True)` — a plan, printed, with nothing moved."""

    def _dry_run(self, **kwargs) -> int:
        return handlers.cmd_requeue_claims(older_than=THRESHOLD, dry_run=True,
                                           **kwargs)

    def test_a_dry_run_reports_the_plan_and_never_reaches_the_provider(self):
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._dry_run(), 0)
        self.assertEqual(self.provider.requeues, [],
                         "a dry run handed a requeue to the provider")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._pending_names(), [])
        logged = self._logged()
        self.assertIn(f"would requeue 001-a (48h) owner={PEER}", logged)
        self.assertIn("dry run: 1 of 1 claim(s) at or over 6h would move to "
                      "pending/", logged)

    def test_a_dry_run_leaves_the_claim_and_its_owner_intact(self):
        self._plant("001-a.md", owner=OWNER, hours_old=STALE_HOURS)
        self._dry_run()
        self.assertEqual(self._owner_of("001-a.md"), OWNER)
        self.assertEqual(self._record_names(), ["001-a.json"])

    def test_a_dry_run_counts_an_unattributable_claim_as_refused(self):
        """The refusal is decided before the move, so a dry run shows the real plan."""
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self.assertEqual(self._dry_run(), 0)
        logged = self._logged()
        self.assertIn("not requeueing 002-b (48h)", logged)
        self.assertIn("dry run: 1 of 2 claim(s) at or over 6h would move to "
                      "pending/", logged)

    def test_a_forced_dry_run_plans_the_unattributable_claim(self):
        self._plant("002-b.md", hours_old=STALE_HOURS)
        self.assertEqual(self._dry_run(force=True), 0)
        logged = self._logged()
        self.assertIn(f"would requeue 002-b (48h) owner={OWNER_UNKNOWN}", logged)
        self.assertIn("dry run: 1 of 1 claim(s) at or over 6h would move to "
                      "pending/", logged)
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self.provider.requeues, [])

    def test_a_dry_run_reports_zero_when_no_claim_is_old_enough(self):
        self._plant("003-c.md", owner=OWNER, hours_old=YOUNG_HOURS)
        self.assertEqual(self._dry_run(), 0)
        self.assertIn("dry run: 0 of 1 claim(s) at or over 6h would move to "
                      "pending/", self._logged())


class EmptyReclaimTest(_WiredFixture):
    """An empty `claimed/` is the healthy case, not an error, for every flag."""

    def test_an_empty_sweep_returns_zero_under_every_flag_combination(self):
        for kwargs in ({}, {"force": True}, {"dry_run": True},
                       {"dry_run": True, "force": True}):
            with self.subTest(**kwargs):
                self.assertEqual(
                    handlers.cmd_requeue_claims(older_than=THRESHOLD, **kwargs), 0)
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])

    def test_an_empty_sweep_reports_its_own_zero(self):
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        self.assertIn("requeued 0 of 0", self._logged())

    def test_an_empty_dry_run_plans_nothing(self):
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD,
                                                     dry_run=True), 0)
        self.assertIn("dry run: 0 of 0 claim(s) at or over 6h would move to "
                      "pending/", self._logged())

    def test_claims_too_young_to_select_leave_an_empty_sweep(self):
        """Nothing moved, and the report says none was selected — not "0 of 1"."""
        self._plant("003-c.md", owner=OWNER, hours_old=YOUNG_HOURS)
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        self.assertEqual(self._claimed_names(), ["003-c.md"])
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])
        self.assertIn("requeued 0 of 0", self._logged())

    def test_an_orphan_legacy_sidecar_is_not_a_claim(self):
        """A legacy sidecar with no markdown under it is not swept as work.

        It is still metadata, so the reclaim pass retires it: the sweep
        migrates it by task id, the orphan view reports it, and the clean
        drops the claim (slice 4, §5.8) — but no markdown is invented for
        it, so `pending/` stays empty and the sweep counts zero claims.
        """
        (self.claimed / "004-d.md.claim.json").write_text('{"owner": "x"}')
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        logged = self._logged()
        self.assertIn("requeued 0 of 0", logged)
        self.assertIn("orphan claim record 004-d", logged)
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])
        self.assertEqual(self._legacy_sidecar_names(), [],
                         "a migrated sidecar outlived its record")
        self.assertEqual(self._record_names(), [],
                         "the orphan claim outlived its clean")


class OwnershipAwareForceTest(_WiredFixture):
    """`cmd_requeue_claims(force=…)` — the operator override, and its bounds."""

    def _requeue(self, **kwargs) -> int:
        return handlers.cmd_requeue_claims(older_than=THRESHOLD, **kwargs)

    def test_an_owned_claim_is_handed_back_naming_its_recorded_owner(self):
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(), 0)
        self.assertEqual(self.provider.requeues,
                         [{"owner": PEER, "force": False, "moved": True}],
                         "the operator requeue did not name the recorded owner")
        logged = self._logged()
        self.assertIn(f"requeued 001-a (48h) owner={PEER}", logged)
        self.assertIn("requeued 1 of 1", logged)
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [],
                         "the claim record outlived the claim it described")

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
        self.assertEqual(self._record_text("003-c.md"), "{ not json",
                         "a refusal rewrote the evidence it refused on")

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
                         "a forced requeue left an owner behind")
        self.assertIn(f"requeued 002-b (48h) owner={OWNER_UNKNOWN}", self._logged())
        self.assertIn("requeued 2 of 2", self._logged())

    def test_force_moves_a_claim_held_by_another_owner_and_names_that_owner(self):
        """Force is an operator's decision, so a peer's claim moves too — visibly."""
        self._plant("001-a.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual(self.provider.requeues,
                         [{"owner": PEER, "force": True, "moved": True}])
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertIn(f"requeued 001-a (48h) owner={PEER}", self._logged())

    def test_force_reaches_no_further_than_the_age_bound(self):
        self._plant("004-d.md", hours_old=YOUNG_HOURS)
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual(self._claimed_names(), ["004-d.md"])
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self.provider.requeues, [])
        self.assertIn("requeued 0 of 0", self._logged())

    def test_a_forced_requeue_never_overwrites_a_pending_name(self):
        claim = self._plant("002-b.md", hours_old=STALE_HOURS)
        original = claim.read_text()
        (self.pending / "002-b.md").write_text("pending original")
        self.assertEqual(self._requeue(force=True), 0)
        self.assertEqual((self.pending / "002-b.md").read_text(),
                         "pending original")
        self.assertEqual((self.pending / "002-b-requeued.md").read_text(), original)


class SlugMatchingTest(_WiredFixture):
    """A task id is `_slug`-ified; a claim filename is not. Both sides must match.

    `list_claims()` hands the handler a `Task` whose id has been through `_slug`
    (dots and other punctuation become `_`, and the id stops at 60 characters),
    and the handler looks the claim up again with only that id — for the age and
    for the requeue. A name that survives neither step unchanged is the case
    that silently returns "no such claim" and reports an empty sweep.
    """

    def test_a_dotted_claim_name_is_aged_and_requeued_under_its_own_filename(self):
        self._plant("003.keep.x.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        self.assertEqual(self._pending_names(), ["003.keep.x.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertIn(f"requeued 003_keep_x (48h) owner={PEER}", self._logged())
        self.assertIn("requeued 1 of 1", self._logged())

    def test_a_dotted_claim_name_is_listed_by_its_slug_in_status(self):
        self._plant("003.keep.x.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertIn(_row(CLAIMED_LOCATION, 1, "003_keep_x (48h)"),
                      self._status_rows())

    def test_a_name_past_the_slug_limit_is_still_matched_and_reported_short(self):
        long_name = "t" + "x" * 80 + ".md"
        self._plant(long_name, owner=PEER, hours_old=STALE_HOURS)
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        self.assertEqual(self._pending_names(), [long_name],
                         "a requeued claim lost its original filename")
        self.assertEqual(self._claimed_names(), [])
        self.assertIn(f"requeued {'t' + 'x' * 59} (48h)", self._logged())

    def test_a_slug_that_matches_no_claim_leaves_the_sweep_empty(self):
        """The negative control: the tests above match by slug, not by luck."""
        self._plant("003.keep.x.md", owner=PEER, hours_old=STALE_HOURS)
        self.assertIsNone(self.provider.requeue_claim("003-keep-x", owner=PEER),
                          "a hyphenated name must not slug-match a dotted claim")
        self.assertEqual(self._claimed_names(), ["003.keep.x.md"])


if __name__ == "__main__":
    unittest.main()
