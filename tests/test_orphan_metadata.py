"""Slice 4 — legacy-orphan migration, safe cleanup, and the `002-…` defect.

A metadata record is keyed by the task, so a sidecar whose task markdown is
gone is sighted by no task read: nothing migrates it, nothing cleans it,
and (in the legacy shape) an orphan `claimed/N.md.claim.json` sat in the
queue blocking stale-claim handling forever. This file owns the three
promises that close that defect class
(single-metadata-record-per-task-no-orphan-sidecars):

  * FR-E2/FR-E5 — `task_record.sweep_legacy` migrates every legacy sidecar
    by task id, orphans included (slug key proven with a name whose slug
    differs from its stem), retires the files the record speaks for, is
    idempotent, and never deletes a legacy file that is still the only
    readable metadata of a task;
  * §5.8 — an orphan claim record with no markdown anywhere is reported
    through `list_orphan_claims`, cleanable through `clean_orphan_claim`
    (a `github` section survives the clean), and is never a task: it stays
    out of `fetch_pending`, `count_pending`, `list_claims`,
    `_default_check_pending`, `cmd_status` and the board (FR-A4);
  * FR-E4 at the handler edge — `cmd_requeue_claims` is the defined
    hygiene path: it migrates, reports and cleans, and a second run finds
    nothing (convergence); `--dry-run` plans and touches nothing.

Every fixture is a temp dir; the live queue is never opened.
"""
from __future__ import annotations

import contextlib
import io
import json
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
from harness.core.providers import DirectoryTaskProvider  # noqa: E402
from harness.core.stats import StatsStore  # noqa: E402
from tests.legacy_sidecars import SyncLinkage, write_legacy_linkage  # noqa: E402
from harness.core.syncd import _default_check_pending  # noqa: E402
from harness.workflow.task_lifecycle import (  # noqa: E402
    CLAIMED_LOCATION,
    QUEUE_LOCATIONS_ALL,
)

OWNER = "run-1111-abcd"
STALE_HOURS = 48
THRESHOLD = 6.0
NOW = 1_700_000_000.0


def _seed_legacy_claim_sidecar(path: Path, owner: str = OWNER,
                               claimed_at: float = NOW) -> Path:
    """A legacy `X.md.claim.json` at `path` (the file need no neighbour)."""
    path.write_text(json.dumps({"version": 1, "owner": owner,
                                "claimed_at": claimed_at,
                                "claim_file": path.name}))
    return path


class _QueueFixture(unittest.TestCase):
    """A temp queue root with every location directory present."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="slice4-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for sub in QUEUE_LOCATIONS_ALL:
            (self.dir / sub).mkdir()
        self.messages: list[str] = []

    def _record(self, task_id: str) -> Path:
        return task_record.record_path(self.dir, task_id)

    def _legacy_files(self) -> list[str]:
        found = []
        for sub in QUEUE_LOCATIONS_ALL:
            for p in (self.dir / sub).rglob("*"):
                if p.is_file() and task_record.is_legacy_metadata_name(p.name):
                    found.append(str(p.relative_to(self.dir)))
        return sorted(found)


class LegacyOrphanSweepTest(_QueueFixture):
    """`sweep_legacy`: migrate every sidecar by task id, retire what is safe."""

    def test_orphan_legacy_claim_migrates_by_task_id(self):
        # The `002-…` defect shape: a claim sidecar whose markdown is gone.
        sidecar = _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION
            / "002-live-instruction-injection.md.claim.json")
        self.assertEqual(task_record.sweep_legacy(self.dir),
                         ["002-live-instruction-injection"])
        claim = task_record.read_record(self.dir,
                                        "002-live-instruction-injection").claim
        self.assertIsNotNone(claim, "orphan ownership must survive migration")
        self.assertEqual(claim.owner, OWNER)
        self.assertFalse(sidecar.exists())

    def test_orphan_record_key_is_the_slug_not_the_stem(self):
        # A name whose slug differs from its stem proves the file-name key
        # is mapped through `_slug`, not used verbatim.
        _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "002 legacy_orphan name.md.claim.json")
        task_record.sweep_legacy(self.dir)
        record = self.dir / task_record.META_DIR_NAME / "002_legacy_orphan_name.json"
        self.assertTrue(record.is_file(), record)
        claim = task_record.read_record(self.dir, "002 legacy_orphan name").claim
        self.assertEqual(claim.owner, OWNER)

    def test_sweep_migrates_live_files_task_dirs_and_orphans_together(self):
        pending = self.dir / "pending" / "010-live.md"
        pending.write_text("# live\n")
        gh = pending.with_name(pending.name + ".gh.json")
        write_legacy_linkage(gh, SyncLinkage(issue=7, repo="o/r"))
        task_dir = self.dir / "done" / "011-finished"
        task_dir.mkdir()
        (task_dir / "gh.json").write_text(json.dumps({"issue": 8, "repo": "o/r"}))
        _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "012-gone.md.claim.json")

        migrated = sorted(task_record.sweep_legacy(self.dir))

        self.assertEqual(migrated, ["010-live", "011-finished", "012-gone"])
        self.assertEqual(task_record.read_linkage(self.dir, "010-live").issue, 7)
        self.assertEqual(task_record.read_linkage(self.dir, "011-finished").issue, 8)
        self.assertEqual(
            task_record.read_record(self.dir, "012-gone").claim.owner, OWNER)
        self.assertEqual(self._legacy_files(), [])

    def test_sweep_is_idempotent_and_leaves_a_clean_queue_untouched(self):
        _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "012-gone.md.claim.json")
        task_record.sweep_legacy(self.dir)
        self.assertEqual(task_record.sweep_legacy(self.dir), [])
        # A queue with no legacy files at all gains nothing from a sweep.
        clean = Path(tempfile.mkdtemp(prefix="slice4-clean-"))
        self.addCleanup(shutil.rmtree, clean, ignore_errors=True)
        (clean / CLAIMED_LOCATION).mkdir()
        self.assertEqual(task_record.sweep_legacy(clean), [])
        self.assertFalse((clean / task_record.META_DIR_NAME).exists())

    def test_cleanup_never_deletes_the_only_readable_metadata(self):
        # A readable legacy claim whose record cannot be written (the `.meta`
        # path is blocked by a plain file) must stay exactly where it is.
        blocked = self.dir / task_record.META_DIR_NAME
        blocked.write_text("not a directory")
        sidecar = _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "013-unwritable.md.claim.json")
        task_record.sweep_legacy(self.dir)
        self.assertTrue(sidecar.is_file(),
                        "the only readable metadata was deleted")
        claim = task_record.read_record(self.dir, "013-unwritable").claim
        self.assertEqual(claim.owner, OWNER)

    def test_corrupt_legacy_sidecar_is_left_in_place(self):
        # Nothing readable to adopt: the sweep keeps the file (audit trail)
        # and the task reads unowned, never raising.
        sidecar = self.dir / CLAIMED_LOCATION / "014-corrupt.md.claim.json"
        sidecar.write_text("{ not json")
        task_record.sweep_legacy(self.dir)
        self.assertTrue(sidecar.is_file())
        self.assertIsNone(task_record.read_record(self.dir,
                                                  "014-corrupt").claim)


class OrphanClaimViewTest(_QueueFixture):
    """`list_orphan_claims` / `clean_orphan_claim`: the §5.8 operator view."""

    def _provider(self) -> DirectoryTaskProvider:
        return DirectoryTaskProvider(self.dir / "pending",
                                     self.dir / CLAIMED_LOCATION,
                                     log=self.messages.append)

    def test_claim_record_without_markdown_anywhere_is_an_orphan(self):
        task_record.set_claim(self.dir, "020-gone", OWNER,
                              claimed_at=NOW - STALE_HOURS * 3600)
        orphans = task_record.list_orphan_claims(self.dir)
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0].task_id, "020-gone")
        self.assertEqual(orphans[0].owner, OWNER)
        self.assertAlmostEqual(orphans[0].claimed_at, NOW - STALE_HOURS * 3600)
        self.assertEqual(orphans[0].record, self._record("020-gone"))

    def test_tasks_anywhere_in_the_queue_are_not_orphans(self):
        held = self.dir / CLAIMED_LOCATION / "021-held.md"
        held.write_text("# held\n")
        task_record.set_claim(self.dir, "021-held", OWNER)
        waiting = self.dir / "pending" / "022-waiting.md"
        waiting.write_text("# waiting\n")
        task_record.set_claim(self.dir, "022-waiting", OWNER)
        done = self.dir / "done" / "023-done"
        done.mkdir()
        task_record.set_claim(self.dir, "023-done", OWNER)
        self.assertEqual(task_record.list_orphan_claims(self.dir), [])

    def test_review_summary_does_not_keep_a_claim_alive(self):
        # `review/<id>.md` is the terminal report, not the task; a claim
        # record whose only namesake is a summary describes no work.
        (self.dir / "review" / "024-summary.md").write_text("## summary\n")
        task_record.set_claim(self.dir, "024-summary", OWNER)
        self.assertEqual([o.task_id
                          for o in task_record.list_orphan_claims(self.dir)],
                         ["024-summary"])

    def test_linkage_only_and_corrupt_records_are_not_orphans(self):
        task_record.write_linkage(self.dir, "025-linked",
                                  SyncLinkage(issue=1, repo="o/r"))
        corrupt = self._record("026-corrupt")
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{ not json")
        self.assertEqual(task_record.list_orphan_claims(self.dir), [])

    def test_cleaning_an_orphan_keeps_the_github_section(self):
        task_record.write_linkage(self.dir, "027-both",
                                  SyncLinkage(issue=2, repo="o/r"))
        task_record.set_claim(self.dir, "027-both", OWNER)
        orphan = task_record.list_orphan_claims(self.dir)[0]
        self.assertTrue(self._provider().clean_orphan_claim(orphan))
        record = task_record.read_record(self.dir, "027-both")
        self.assertIsNone(record.claim)
        self.assertEqual(record.github.issue, 2)

    def test_cleaning_an_orphan_drops_the_record_when_nothing_is_left(self):
        task_record.set_claim(self.dir, "028-claim-only", OWNER)
        orphan = task_record.list_orphan_claims(self.dir)[0]
        self.assertTrue(self._provider().clean_orphan_claim(orphan))
        self.assertFalse(self._record("028-claim-only").exists())


class MetadataNonEnumerationTest(_QueueFixture):
    """FR-A4: metadata — new or leftover — is never enumerable as a task."""

    def setUp(self):
        super().setUp()
        # New records only: one owned, one linked, one orphan claim.
        task_record.set_claim(self.dir, "030-orphan", OWNER, claimed_at=NOW)
        task_record.write_linkage(self.dir, "031-linked",
                                  SyncLinkage(issue=3, repo="o/r"))
        # Legacy leftovers no read has retired yet (corrupt: unadoptable).
        (self.dir / "pending" / "032-x.md.gh.json").write_text("{ nope")
        (self.dir / CLAIMED_LOCATION / "033-y.md.claim.json").write_text("{ nope")

    def test_fetch_count_and_claim_listings_see_zero_tasks(self):
        provider = DirectoryTaskProvider(self.dir / "pending",
                                         self.dir / CLAIMED_LOCATION,
                                         log=self.messages.append)
        self.assertEqual(provider.fetch_pending(), [])
        self.assertEqual(provider.count_pending(), 0)
        self.assertEqual(provider.list_claims(), [])
        self.assertEqual(provider.list_owned_claims(), [])

    def test_the_syncd_pending_check_reports_no_phantom_work(self):
        work = Path(tempfile.mkdtemp(prefix="slice4-work-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        pending = work / "queue" / "pending"
        pending.mkdir(parents=True)
        (pending / "034-z.md.gh.json").write_text('{"issue": 1}')
        (pending / "035-orphan.md.claim.json").write_text(
            json.dumps({"owner": OWNER, "claimed_at": NOW}))
        self.assertFalse(_default_check_pending(work))

    def test_status_and_the_board_render_no_metadata_as_tasks(self):
        provider = DirectoryTaskProvider(self.dir / "pending",
                                         self.dir / CLAIMED_LOCATION,
                                         log=self.messages.append)
        cfg = types.SimpleNamespace(harness_execution_and_queue_dir=self.dir, queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, provider, None,
                 lambda line="": self.messages.append(line))
        with mock.patch.object(handlers, "build", lambda *a, **k: wired):
            self.assertEqual(handlers.cmd_status(), 0)
            board = contextlib.redirect_stdout(io.StringIO())
            with board as out:
                self.assertEqual(handlers.cmd_board(), 0)
        rows = "\n".join(self.messages[:len(QUEUE_LOCATIONS_ALL)])
        self.assertIn(f"{CLAIMED_LOCATION:<10} (0): -", rows)
        self.assertIn(f"{'pending':<10} (0): -", rows)
        for name in ("032-x.md.gh.json", "033-y.md.claim.json",
                     "030-orphan", "031-linked"):
            self.assertNotIn(name.split(".")[0], out.getvalue(),
                             "metadata surfaced as a board task")


class RequeueClaimsOrphanTest(_QueueFixture):
    """`cmd_requeue_claims` is the defined hygiene path: migrate, report,
    clean, converge."""

    def setUp(self):
        super().setUp()
        self.provider = DirectoryTaskProvider(
            self.dir / "pending", self.dir / CLAIMED_LOCATION,
            log=self.messages.append)
        cfg = types.SimpleNamespace(harness_execution_and_queue_dir=self.dir, queue_dir=self.dir,
                                    logs_dir=self.dir / "logs",
                                    stats_path=self.dir / "stats.jsonl")
        wired = (cfg, StatsStore(cfg.stats_path), None, self.provider, None,
                 lambda line="": self.messages.append(line))
        patcher = mock.patch.object(handlers, "build", lambda *a, **k: wired)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _logged(self) -> str:
        return " | ".join(self.messages)

    def test_legacy_orphan_migrates_reports_and_cleans_in_one_run(self):
        _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION
            / "002-live-instruction-injection.md.claim.json",
            claimed_at=time.time() - STALE_HOURS * 3600)
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        logged = self._logged()
        self.assertIn("migrated legacy metadata for 1 task(s)", logged)
        self.assertIn("cleaned orphan claim record "
                      "002-live-instruction-injection", logged)
        self.assertFalse((self.dir / CLAIMED_LOCATION
                          / "002-live-instruction-injection.md.claim.json")
                         .exists())
        self.assertFalse(self._record("002-live-instruction-injection").exists())

    def test_a_second_run_converges_and_reports_nothing(self):
        _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "002-gone.md.claim.json",
            claimed_at=time.time() - STALE_HOURS * 3600)
        handlers.cmd_requeue_claims(older_than=THRESHOLD)
        self.messages.clear()
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        logged = self._logged()
        self.assertNotIn("migrated", logged)
        self.assertNotIn("orphan", logged)

    def test_dry_run_plans_the_orphan_clean_and_touches_nothing(self):
        task_record.set_claim(self.dir, "040-orphan", OWNER,
                              claimed_at=time.time() - STALE_HOURS * 3600)
        self.assertEqual(
            handlers.cmd_requeue_claims(older_than=THRESHOLD, dry_run=True), 0)
        logged = self._logged()
        self.assertIn("would clean orphan claim record 040-orphan", logged)
        self.assertIn(f"owner={OWNER}", logged)
        claim = task_record.read_record(self.dir, "040-orphan").claim
        self.assertIsNotNone(claim, "a dry run cleared an orphan")

    def test_dry_run_migrates_nothing_and_still_reports_the_legacy_orphan(
            self):
        # An inspection run leaves the queue byte-identical, yet still names
        # the orphan whose ownership data is only a legacy sidecar.
        sidecar = _seed_legacy_claim_sidecar(
            self.dir / CLAIMED_LOCATION / "002-gone.md.claim.json",
            claimed_at=time.time() - STALE_HOURS * 3600)
        self.assertEqual(
            handlers.cmd_requeue_claims(older_than=THRESHOLD, dry_run=True), 0)
        logged = self._logged()
        self.assertIn("would migrate legacy metadata for 1 task(s)", logged)
        self.assertIn("would clean orphan claim record 002-gone", logged)
        self.assertTrue(sidecar.is_file(), "a dry run retired a legacy file")
        self.assertFalse(self._record("002-gone").exists(),
                         "a dry run wrote a record")

    def test_orphan_cleaning_keeps_the_linkage_section(self):
        task_record.write_linkage(self.dir, "041-linked-orphan",
                                  SyncLinkage(issue=4, repo="o/r"))
        task_record.set_claim(self.dir, "041-linked-orphan", OWNER,
                              claimed_at=time.time() - STALE_HOURS * 3600)
        handlers.cmd_requeue_claims(older_than=THRESHOLD)
        record = task_record.read_record(self.dir, "041-linked-orphan")
        self.assertIsNone(record.claim)
        self.assertEqual(record.github.issue, 4)

    def test_a_young_orphan_is_outside_the_older_than_bound(self):
        task_record.set_claim(self.dir, "042-new", OWNER,
                              claimed_at=time.time())
        self.assertEqual(
            handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        self.assertNotIn("orphan", self._logged())
        claim = task_record.read_record(self.dir, "042-new").claim
        self.assertEqual(claim.owner, OWNER)

    def test_live_claims_are_requeued_not_treated_as_orphans(self):
        claim_file = self.dir / CLAIMED_LOCATION / "043-live.md"
        claim_file.write_text("# live\n")
        task_record.set_claim(self.dir, "043-live", OWNER,
                              claimed_at=time.time() - STALE_HOURS * 3600)
        stamp = time.time() - (STALE_HOURS * 3600) - 1
        import os
        os.utime(claim_file, (stamp, stamp))
        self.assertEqual(handlers.cmd_requeue_claims(older_than=THRESHOLD), 0)
        logged = self._logged()
        self.assertIn("requeued 043-live", logged)
        self.assertNotIn("orphan", logged)
        self.assertTrue((self.dir / "pending" / "043-live.md").is_file())


if __name__ == "__main__":
    unittest.main()
