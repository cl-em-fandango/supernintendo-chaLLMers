"""T51 — a claim records who holds it, and only that owner may hand it back.

Claims were identified by filename and age alone, so a cleanup path could not
tell its own claim from one held by another live invocation and handing back
"the leftovers" could steal a running peer's work. The directory provider takes
an optional owner id at claim time and records it in the task's single
metadata record (`task_record.py`, `<queue>/.meta/<task-id>.json`); a requeue
that names an owner moves only claims recorded against that owner.

Covered here:
  * the record the claim path writes — one task-keyed document, atomic write,
    unowned on absent/corrupt reads, and no legacy `.claim.json` created;
  * claim rename + record write is rollback-safe: a record that cannot be
    written puts the markdown back in pending/ and raises, leaving no owned
    record anywhere;
  * two owners — owner A cannot requeue owner B's claim, by Task or by name,
    one-at-a-time or in bulk;
  * claims with no readable `claim` section read as `owner=unknown` and are
    refused an ownership-checked requeue rather than silently stolen;
  * the record across transitions: a `-requeued` collision suffix re-keys it
    onto the moved task without touching a live claim already at that id, a
    claim race loser records nothing, and a claim write never drops the
    task's `github` section — legacy linkage included, which stays on disk
    until the linkage concern is converted to the record;
  * an owner-less requeue keeps the pre-ownership behaviour (the CLI is not
    generating owner ids yet — that is T52).
"""
from __future__ import annotations

import dataclasses
import glob
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import task_record  # noqa: E402
from harness.core.claim_metadata import (  # noqa: E402
    OWNER_UNKNOWN,
    ClaimMetadataError,
)
from harness.core.providers import (  # noqa: E402
    Claim,
    DirectoryTaskProvider,
    Task,
    TaskProvider,
)
from harness.core.sync_inbound import scan_queue  # noqa: E402
from tests.legacy_sidecars import (  # noqa: E402
    SyncLinkage,
)


class _QueueFixture(unittest.TestCase):
    """A temp pending/claimed pair and the listing shorthands the tests read."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t51-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.messages: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.messages.append)

    def _pending_names(self):
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self):
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def _record_names(self):
        """The records in the queue's `.meta/` store, by file name."""
        meta = self.dir / task_record.META_DIR_NAME
        return sorted(p.name for p in meta.glob("*.json")) if meta.is_dir() \
            else []

    def _legacy_sidecar_names(self):
        """Any legacy sidecar anywhere under the queue root."""
        return sorted(Path(p).name for p in
                      glob.glob(str(self.dir / "**" / "*.claim.json")))

    def _owner_of(self, name: str) -> str:
        """The owner the task's record names, `OWNER_UNKNOWN` when unreadable."""
        claim = task_record.read_record(self.dir, Path(name).stem).claim
        return claim.owner if claim is not None else OWNER_UNKNOWN

    def _logged(self):
        return " | ".join(self.messages)

    def _claim_one(self, name: str, body: str, owner: str) -> Task:
        (self.pending / name).write_text(body)
        return self.provider.fetch_pending(claim=True, limit=1, owner=owner)[0]


class RecordFormatTest(_QueueFixture):
    """The record the claim path writes: where it lives, what it holds."""

    def test_claim_writes_one_record_keyed_by_the_task_id(self):
        self._claim_one("001-a.md", "A", "run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._record_names(), ["001-a.json"])
        self.assertEqual(self._legacy_sidecar_names(), [],
                         "a live path wrote a legacy sidecar")

        payload = json.loads(
            task_record.record_path(self.dir, "001-a").read_text())
        self.assertEqual(payload["version"],
                         task_record.RECORD_SCHEMA_VERSION)
        self.assertEqual(payload["claim"]["owner"], "run-a")
        self.assertIsNone(payload["github"],
                          "an unlinked task grew a linkage section")

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        before = time.time()
        task_record.set_claim(self.dir, "001-a", "run-a")
        meta = self.dir / task_record.META_DIR_NAME
        self.assertEqual(sorted(p.name for p in meta.glob("*.tmp*")), [])
        self.assertEqual(sorted(p.name for p in meta.glob("*.tmp")), [])
        self.assertGreaterEqual(
            task_record.read_record(self.dir, "001-a").claim.claimed_at,
            before - 1)

    def test_written_owner_and_time_read_back(self):
        task_record.set_claim(self.dir, "001-a", "run-a", claimed_at=1234.5)
        claim = task_record.read_record(self.dir, "001-a").claim
        self.assertEqual((claim.owner, claim.claimed_at), ("run-a", 1234.5))

    def test_absent_record_reads_unknown(self):
        self.assertEqual(self._owner_of("nope.md"), OWNER_UNKNOWN)
        self.assertIsNone(task_record.read_record(self.dir, "nope").claim)
        self.assertFalse(task_record.record_path(self.dir, "nope").exists())

    def test_corrupt_records_read_unknown(self):
        cases = {
            "not json at all": "owner = run-a",
            "json but not an object": '["run-a"]',
            "no owner key": '{"claim": {"claimed_at": 1.0}}',
            "blank owner": '{"claim": {"owner": ""}}',
            "non-string owner": '{"claim": {"owner": 7}}',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                record = task_record.record_path(self.dir, "001-a")
                record.parent.mkdir(parents=True, exist_ok=True)
                record.write_text(text)
                self.assertEqual(self._owner_of("001-a.md"), OWNER_UNKNOWN)

    def test_a_bad_timestamp_does_not_make_the_owner_unknown(self):
        record = task_record.record_path(self.dir, "001-a")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"version": 1, "github": null, '
                          '"claim": {"owner": "run-a", "claimed_at": "soon"}}')
        claim = task_record.read_record(self.dir, "001-a").claim
        self.assertEqual(claim.owner, "run-a")
        self.assertEqual(claim.claimed_at, 0.0)


class OwnedClaimTest(_QueueFixture):
    """`fetch_pending(claim=True, owner=...)` and the ownership-checked requeue."""

    def test_owned_claim_is_reported_as_a_claim_dataclass(self):
        task = self._claim_one("001-a.md", "A", "run-a")
        claims = self.provider.list_owned_claims()
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.task, task)
        self.assertEqual(claim.filename, "001-a.md")
        self.assertEqual(claim.owner, "run-a")
        self.assertAlmostEqual(claim.claimed_at, time.time(), delta=60)
        self.assertEqual(claim.meta_path,
                         task_record.record_path(self.dir, "001-a"))

    def test_claim_dataclass_shape(self):
        """T52/T53 read these fields; the names are part of the contract."""
        self.assertEqual([f.name for f in dataclasses.fields(Claim)],
                         ["task", "filename", "owner", "claimed_at", "meta_path"])

    def test_sidecars_are_invisible_to_the_task_view(self):
        """`list_claims()` globs markdown; a sidecar must never become a task."""
        self._claim_one("001-a.md", "A", "run-a")
        self.assertEqual([t.source for t in self.provider.list_claims()],
                         ["claimed:001-a.md"])
        self.assertGreaterEqual(self.provider.claim_age_hours("001-a.md"), 0)

    def test_owner_a_cannot_requeue_owner_b_s_claim(self):
        task_a = self._claim_one("001-a.md", "A", "run-a")
        task_b = self._claim_one("002-b.md", "B", "run-b")

        self.assertIsNone(self.provider.requeue_claim(task_b, owner="run-a"))
        self.assertEqual(self._claimed_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._pending_names(), [], "foreign claim was stolen")
        self.assertIn("not requeueing 002-b.md", self._logged())
        self.assertIn("run-b", self._logged())

        self.assertEqual(self.provider.requeue_claim(task_a, owner="run-a"),
                         str(self.pending / "001-a.md"))
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self._pending_names(), ["001-a.md"])

    def test_ownership_is_checked_for_a_filename_lookup_too(self):
        self._claim_one("001-a.md", "A", "run-a")
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner="run-b"))
        self.assertIsNone(self.provider.requeue_claim("001-a", owner="run-b"))
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self.provider.requeue_claim("001-a.md", owner="run-a"),
                         str(self.pending / "001-a.md"))

    def test_requeue_all_only_moves_the_named_owner(self):
        self._claim_one("001-a.md", "A", "run-a")
        self._claim_one("002-b.md", "B", "run-b")
        self.assertEqual(self.provider.requeue_all_claims(owner="run-a"), ["001-a.md"])
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self._record_names(), ["002-b.json"])

    def test_requeue_without_an_owner_still_moves_any_claim(self):
        """The pre-ownership call is unchecked; the CLI only names an owner
        once T52 wires owner ids through the run commands."""
        self._claim_one("001-a.md", "A", "run-a")
        self.assertEqual(self.provider.requeue_claim("001-a.md"),
                         str(self.pending / "001-a.md"))
        self.assertEqual(self._claimed_names(), [])

    def test_requeued_claim_leaves_no_owner_behind(self):
        self._claim_one("001-a.md", "A", "run-a")
        self.provider.requeue_claim("001-a.md", owner="run-a")
        self.assertEqual(self._record_names(), [])
        self.assertEqual(self._owner_of("001-a.md"), OWNER_UNKNOWN)

    def test_claim_without_metadata_is_unknown_and_refused(self):
        (self.claimed / "001-a.md").write_text("A")
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner="run-a"))
        self.assertEqual(self.provider.requeue_all_claims(owner="run-a"), [])
        self.assertEqual(self._claimed_names(), ["001-a.md"])

    def test_corrupt_metadata_is_unknown_and_refused(self):
        (self.claimed / "001-a.md").write_text("A")
        record = task_record.record_path(self.dir, "001-a")
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("{ not json")
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner="run-a"))
        self.assertEqual(self._claimed_names(), ["001-a.md"])

    def test_release_claim_clears_the_claim_record(self):
        task = self._claim_one("001-a.md", "A", "run-a")
        self.provider.release_claim(task)
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [])


class ClaimMetadataRollbackTest(_QueueFixture):
    """A claim without an owner is worse than no claim: it rolls back."""

    def test_a_record_write_failure_raises_a_named_error(self):
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                task_record.set_claim(self.dir, "001-a", "run-a")
        meta = self.dir / task_record.META_DIR_NAME
        self.assertEqual(sorted(p.name for p in meta.glob("*.tmp*")), [])
        self.assertEqual(self._owner_of("001-a.md"), OWNER_UNKNOWN)

    def test_failed_record_write_returns_the_markdown_to_pending(self):
        (self.pending / "001-a.md").write_text("original body")
        with mock.patch.object(task_record, "set_claim",
                               side_effect=ClaimMetadataError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), [], "claim survived its rollback")
        self.assertEqual(self._record_names(), [])
        self.assertEqual(self._legacy_sidecar_names(), [])
        self.assertEqual(self._owner_of("001-a.md"), OWNER_UNKNOWN)
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual((self.pending / "001-a.md").read_text(), "original body")

    def test_a_real_write_failure_rolls_the_claim_back(self):
        """Same path, driven by the filesystem rather than a patched call."""
        (self.pending / "001-a.md").write_text("original body")
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._record_names(), [])
        self.assertEqual(self._legacy_sidecar_names(), [])

    def test_only_the_claim_that_failed_is_rolled_back(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        real_set_claim = task_record.set_claim
        calls = []

        def flaky(queue_dir, task_id, owner, claimed_at=None):
            calls.append(task_id)
            if len(calls) == 2:
                raise ClaimMetadataError("disk full")
            return real_set_claim(queue_dir, task_id, owner,
                                  claimed_at=claimed_at)

        with mock.patch.object(task_record, "set_claim", side_effect=flaky):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._record_names(), ["001-a.json"])
        self.assertEqual(self._owner_of("001-a.md"), "run-a")
        self.assertEqual(self._pending_names(), ["002-b.md"])

    def test_a_rollback_that_cannot_move_back_is_logged_and_still_raises(self):
        (self.pending / "001-a.md").write_text("A")
        with mock.patch.object(task_record, "set_claim",
                               side_effect=ClaimMetadataError("disk full")), \
                mock.patch.object(self.provider, "_move_to_pending",
                                  return_value=None):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertIn("could not be rolled back", self._logged())

    def test_claiming_without_an_owner_writes_no_record(self):
        (self.pending / "001-a.md").write_text("A")
        self.provider.fetch_pending(claim=True)
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._record_names(), [])
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)


class ClaimTransitionRecordTest(_QueueFixture):
    """The record across the transitions a claim takes part in.

    Spec §5.2 (a `-requeued` collision suffix re-keys the record so the old
    key is not stranded), §5.9 (tasks `X` and `X-requeued` never share one),
    §5.7 (the loser of a claim race records nothing), FR-D4 (a claim write
    preserves the `github` section, new or legacy) and FR-E2 (a legacy claim
    sidecar is folded in on sight).
    """

    def _linkage(self, issue: int = 7, demo: bool = False) -> SyncLinkage:
        return SyncLinkage(issue=issue, repo="acme/widgets", demo=demo)

    def _claim_then_collide(self) -> None:
        """Task A claimed as 001-a, then a different task re-occupies the name."""
        (self.pending / "001-a.md").write_text("A")
        task_record.write_linkage(self.dir, "001-a", self._linkage())
        self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        (self.pending / "001-a.md").write_text("B")

    def test_claim_preserves_the_github_section_of_the_record(self):
        (self.pending / "001-a.md").write_text("A")
        task_record.write_linkage(self.dir, "001-a", self._linkage(demo=True))
        self.provider.fetch_pending(claim=True, owner="run-a")

        record = task_record.read_record(self.dir, "001-a")
        self.assertEqual(record.claim.owner, "run-a")
        self.assertEqual((record.github.issue, record.github.demo), (7, True),
                         "the claim write clobbered the linkage (FR-D4)")
        self.assertEqual(self._record_names(), ["001-a.json"])

    def test_claim_folds_in_a_legacy_linkage_sidecar(self):
        (self.pending / "001-a.md").write_text("A")
        sidecar = self.pending / "001-a.md.gh.json"
        sidecar.write_text(json.dumps({"issue": 9, "repo": "acme/widgets",
                                       "comment_ids": {"h1": 3}, "demo": True}))
        tasks = self.provider.fetch_pending(claim=True, owner="run-a")

        record = task_record.read_record(self.dir, "001-a")
        self.assertEqual((record.github.issue, record.github.comment_ids),
                         (9, {"h1": 3}))
        self.assertEqual(record.claim.owner, "run-a")
        self.assertEqual(tasks[0].meta, {"demo": True})

    def test_a_claim_read_migrates_a_legacy_linkage_too(self):
        """Both concerns resolve the record, so a claim-path read (what
        `status`, `board` and `requeue-claims` do) retires the legacy
        `.gh.json` into the record (FR-E2/FR-E3) — and the linkage is still
        found by task id, wherever the task sits.
        """
        (self.claimed / "001-a.md").write_text("A")
        sidecar = self.claimed / "001-a.md.gh.json"
        sidecar.write_text(json.dumps({"issue": 9, "repo": "acme/widgets",
                                       "comment_ids": {"h1": 3}}))

        claims = self.provider.list_owned_claims()
        self.assertEqual([c.filename for c in claims], ["001-a.md"])

        self.assertFalse(sidecar.exists(),
                         "a legacy linkage was left beside the task file")
        self.assertEqual(
            task_record.read_linkage(self.dir, "001-a").comment_ids,
            {"h1": 3})
        scanned = [e for e in scan_queue(self.dir) if e.name == "001-a"]
        self.assertEqual(
            [task_record.read_linkage(self.dir, e.name).issue
             for e in scanned], [9])

    def test_a_legacy_claim_sidecar_is_read_and_migrated_on_sight(self):
        (self.claimed / "001-a.md").write_text("A")
        sidecar = self.claimed / "001-a.md.claim.json"
        sidecar.write_text(json.dumps({"owner": "run-legacy",
                                       "claimed_at": 1000.0}))

        claims = self.provider.list_owned_claims()
        self.assertEqual([(c.filename, c.owner) for c in claims],
                         [("001-a.md", "run-legacy")])
        self.assertEqual(self._record_names(), ["001-a.json"])
        self.assertFalse(sidecar.exists(), "legacy sidecar outlived the record")

    def test_requeue_collision_rekeys_the_record_onto_the_moved_task(self):
        self._claim_then_collide()

        moved = self.provider.requeue_claim("001-a.md", owner="run-a")

        self.assertEqual(Path(moved).name, "001-a-requeued.md")
        self.assertEqual(self._pending_names(),
                         ["001-a-requeued.md", "001-a.md"])
        self.assertEqual(self._record_names(), ["001-a-requeued.json"],
                         "the old key was stranded pointing at a phantom")
        record = task_record.read_record(self.dir, "001-a-requeued")
        self.assertIsNone(record.claim, "the ended claim still named an owner")
        self.assertEqual(record.github.issue, 7,
                         "the linkage did not follow the task to its new id")
        self.assertIsNone(task_record.read_record(self.dir, "001-a").github,
                          "the colliding task inherited the linkage")

    def test_a_requeued_task_and_its_colliding_task_keep_separate_records(self):
        self._claim_then_collide()
        self.provider.requeue_claim("001-a.md", owner="run-a")

        # Task B (`001-a`) is claimed by another invocation — the same
        # `set_claim` call `fetch_pending` makes for that task id.
        task_record.set_claim(self.dir, "001-a", "run-b")

        self.assertEqual(sorted(self._record_names()),
                         ["001-a-requeued.json", "001-a.json"])
        self.assertEqual(task_record.read_record(self.dir, "001-a").claim.owner,
                         "run-b")
        requeued = task_record.read_record(self.dir, "001-a-requeued")
        self.assertIsNone(requeued.claim,
                          "the two tasks shared one claim section")
        self.assertEqual(requeued.github.issue, 7)

    def test_requeue_collision_does_not_end_another_tasks_claim(self):
        """§5.9: the `-requeued` key can already be a live claim of its own.

        The claim being ended lives under the source id, so only that record
        is cleared; the record at the collision id belongs to another
        invocation and is left completely alone.
        """
        (self.claimed / "001-a.md").write_text("A")
        (self.claimed / "001-a-requeued.md").write_text("C")
        (self.pending / "001-a.md").write_text("B")   # forces the suffix
        task_record.set_claim(self.dir, "001-a", "run-a")
        task_record.set_claim(self.dir, "001-a-requeued", "run-c")

        moved = self.provider.requeue_claim("001-a.md", owner="run-a")

        self.assertEqual(Path(moved).name, "001-a-requeued.md")
        self.assertEqual(task_record.read_record(
            self.dir, "001-a-requeued").claim.owner, "run-c",
            "the requeue destroyed a live claim it did not hold")
        self.assertIsNone(task_record.read_record(self.dir, "001-a").claim,
                          "the ended claim still named an owner")
        self.assertEqual(
            sorted((c.filename, c.owner) for c in self.provider.list_owned_claims()),
            [("001-a-requeued.md", "run-c")])
        # run-c may still hand its own claim back afterwards.
        self.assertIsNotNone(self.provider.requeue_claim(
            "001-a-requeued.md", owner="run-c"))

    def test_a_claim_race_loser_records_nothing(self):
        """§5.7: the peer that loses the rename takes no task and no record."""
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        real_rename = Path.rename

        def lose(this, other, *args):
            if Path(other).name == "001-a.md":
                raise OSError("peer claimed it first")
            return real_rename(this, other, *args)

        with mock.patch.object(Path, "rename", lose):
            tasks = self.provider.fetch_pending(claim=True, owner="run-a")

        self.assertEqual([t.id for t in tasks], ["002-b"])
        self.assertEqual(self._record_names(), ["002-b.json"],
                         "the loser wrote a record for a claim it never took")
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._owner_of("001-a.md"), OWNER_UNKNOWN)


class NonDirectoryProviderDefaultsTest(unittest.TestCase):
    """A source with no claim concept stays a valid adapter."""

    class NullProvider(TaskProvider):
        def fetch_pending(self) -> list[Task]:
            return []

    def test_ownership_api_defaults(self):
        provider = self.NullProvider()
        self.assertEqual(provider.list_owned_claims(), [])
        self.assertIsNone(provider.requeue_claim(Task(id="x", body=""), owner="run-a"))
        self.assertEqual(provider.requeue_all_claims(owner="run-a"), [])


if __name__ == "__main__":
    unittest.main()
