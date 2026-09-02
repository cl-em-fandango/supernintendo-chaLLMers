"""Slice 1 — the single per-task metadata record (`task_record.py`).

`<queue>/.meta/<task-id>.json` replaces the per-concern sidecars
(`X.md.gh.json`, `X.md.claim.json`, in-dir `gh.json`) with one task-keyed
record. This module is additive: no existing caller is converted yet, so
these tests exercise the record API and the lazy legacy migration in
isolation.

Covered here (spec references from
single-metadata-record-per-task-no-orphan-sidecars):
  * FR-A1 — one record holds both concerns; FR-A2 — the record key is the
    `_slug`-ified task id, proven with a name whose slug differs from its
    file stem;
  * FR-B2 / §5.10 — absent, empty, corrupt, non-object, blank-owner and
    bool-timestamp reads are defensive and never raise;
  * FR-D4 — read-modify-write: `set_claim` preserves `github`,
    `write_linkage` preserves `claim`;
  * FR-D1/D2 — atomic write (no temp litter), failure raises
    `ClaimMetadataError` so the claim rename can roll back;
  * §5.5 — corrupt new record + valid legacy sidecar: legacy read applies,
    then migration repairs; §5.6 — the new record wins over a disagreeing
    legacy sidecar;
  * FR-E1/E2 — legacy sidecars (file suffixes and in-dir `gh.json`) are
    merged on sight, removed only after the new record is durable, and only
    a legacy file the merged record cannot speak for is left on disk;
  * FR-E4 — migration is idempotent and a legacy-free queue is untouched.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.claim_metadata import ClaimMetadataError  # noqa: E402
from tests.legacy_sidecars import SyncLinkage  # noqa: E402
from harness.core import task_record  # noqa: E402
from harness.core.task_record import (  # noqa: E402
    META_DIR_NAME,
    RECORD_SCHEMA_VERSION,
    clear_claim,
    read_linkage,
    read_record,
    record_path,
    set_claim,
    write_linkage,
)


def seed_legacy_gh(task_file: Path, issue: int = 7, repo: str = "o/r",
                   comment_ids: dict | None = None, demo: bool = False) -> Path:
    """A legacy `X.md.gh.json` beside `task_file`."""
    sidecar = task_file.with_name(task_file.name + ".gh.json")
    payload = {"issue": issue, "repo": repo,
               "comment_ids": comment_ids or {}, "demo": demo}
    sidecar.write_text(json.dumps(payload))
    return sidecar


def seed_legacy_claim(claim_file: Path, owner: str = "inv-1",
                      claimed_at: float = 100.0) -> Path:
    """A legacy `X.md.claim.json` beside `claim_file`."""
    sidecar = claim_file.with_name(claim_file.name + ".claim.json")
    sidecar.write_text(json.dumps({"version": 1, "owner": owner,
                                   "claimed_at": claimed_at,
                                   "claim_file": claim_file.name}))
    return sidecar


def seed_legacy_dir_gh(queue: Path, location: str, task_id: str,
                       issue: int = 9) -> Path:
    """A legacy `gh.json` inside a task directory."""
    task_dir = queue / location / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    sidecar = task_dir / "gh.json"
    sidecar.write_text(json.dumps({"issue": issue, "repo": "o/r"}))
    return sidecar


class RecordShapeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name)
        (self.queue / "pending").mkdir()
        (self.queue / "claimed").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_single_record_holds_both_concerns(self):
        """FR-A1: one JSON document, both sections, one file on disk."""
        set_claim(self.queue, "t1", owner="inv-1", claimed_at=42.0)
        write_linkage(self.queue, "t1",
                      SyncLinkage(issue=3, repo="o/r",
                                  comment_ids={"e1": "c1"}, demo=True))
        meta_dir = self.queue / META_DIR_NAME
        self.assertEqual([p.name for p in meta_dir.iterdir()], ["t1.json"])
        payload = json.loads(record_path(self.queue, "t1").read_text())
        self.assertEqual(payload["version"], RECORD_SCHEMA_VERSION)
        self.assertEqual(payload["github"]["issue"], 3)
        self.assertEqual(payload["github"]["comment_ids"], {"e1": "c1"})
        self.assertTrue(payload["github"]["demo"])
        self.assertEqual(payload["claim"]["owner"], "inv-1")
        self.assertEqual(payload["claim"]["claimed_at"], 42.0)

    def test_record_key_is_the_slug_not_the_stem(self):
        """FR-A2: a name whose slug differs from its stem maps to the slug."""
        seed_legacy_claim(self.queue / "claimed" / "my_task name.md",
                          owner="inv-9", claimed_at=5.0)
        (self.queue / "claimed" / "my_task name.md").write_text("t")

        record = read_record(self.queue, "my_task_name")
        self.assertEqual(record.claim.owner, "inv-9")
        self.assertTrue(
            record_path(self.queue, "my_task_name").is_file())
        self.assertEqual(record_path(self.queue, "my_task_name").name,
                         "my_task_name.json")

    def test_record_path_never_derives_from_task_file_name(self):
        """The path depends only on queue + id, not on any file location."""
        self.assertEqual(record_path(self.queue, "abc"),
                         self.queue / META_DIR_NAME / "abc.json")


class DefensiveReadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write_raw(self, text: str) -> None:
        path = record_path(self.queue, "t1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_absent_record_is_unlinked_and_unowned(self):
        record = read_record(self.queue, "t1")
        self.assertIsNone(record.github)
        self.assertIsNone(record.claim)

    def test_empty_record_file(self):
        self._write_raw("")
        record = read_record(self.queue, "t1")
        self.assertIsNone(record.github)
        self.assertIsNone(record.claim)

    def test_corrupt_record_file(self):
        self._write_raw("{not json")
        self.assertIsNone(read_record(self.queue, "t1").github)

    def test_non_object_record_file(self):
        self._write_raw("[1, 2]")
        self.assertIsNone(read_record(self.queue, "t1").claim)

    def test_blank_owner_reads_unowned(self):
        self._write_raw(json.dumps(
            {"version": 1, "github": None, "claim": {"owner": "  "}}))
        # A blank string is present but not a usable owner.
        self.assertIsNone(read_record(self.queue, "t1").claim)
        self._write_raw(json.dumps(
            {"version": 1, "github": None, "claim": {"owner": ""}}))
        self.assertIsNone(read_record(self.queue, "t1").claim)

    def test_bool_claimed_at_reads_as_zero(self):
        self._write_raw(json.dumps(
            {"version": 1, "github": None,
             "claim": {"owner": "inv", "claimed_at": True}}))
        self.assertEqual(read_record(self.queue, "t1").claim.claimed_at, 0.0)

    def test_string_claimed_at_reads_as_zero(self):
        self._write_raw(json.dumps(
            {"version": 1, "github": None,
             "claim": {"owner": "inv", "claimed_at": "soon"}}))
        self.assertEqual(read_record(self.queue, "t1").claim.claimed_at, 0.0)

    def test_github_section_without_issue_dropped(self):
        self._write_raw(json.dumps(
            {"version": 1, "github": {"repo": "o/r"}, "claim": None}))
        self.assertIsNone(read_record(self.queue, "t1").github)


class SectionPreservationTest(unittest.TestCase):
    """FR-D4: every write targets one concern and preserves the other."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_set_claim_preserves_github(self):
        write_linkage(self.queue, "t1", SyncLinkage(issue=4, repo="o/r"))
        set_claim(self.queue, "t1", owner="inv-2")
        record = read_record(self.queue, "t1")
        self.assertEqual(record.github.issue, 4)
        self.assertEqual(record.claim.owner, "inv-2")

    def test_write_linkage_preserves_claim(self):
        set_claim(self.queue, "t1", owner="inv-2", claimed_at=9.0)
        write_linkage(self.queue, "t1", SyncLinkage(issue=5, repo="a/b"))
        record = read_record(self.queue, "t1")
        self.assertEqual(record.claim.owner, "inv-2")
        self.assertEqual(record.claim.claimed_at, 9.0)
        self.assertEqual(record.github.repo, "a/b")

    def test_clear_claim_keeps_github(self):
        write_linkage(self.queue, "t1", SyncLinkage(issue=6, repo="o/r"))
        set_claim(self.queue, "t1", owner="inv")
        self.assertTrue(clear_claim(self.queue, "t1"))
        record = read_record(self.queue, "t1")
        self.assertIsNone(record.claim)
        self.assertEqual(record.github.issue, 6)

    def test_clear_claim_removes_record_when_github_empty(self):
        set_claim(self.queue, "t1", owner="inv")
        self.assertTrue(clear_claim(self.queue, "t1"))
        self.assertFalse(record_path(self.queue, "t1").exists())

    def test_clear_claim_keeps_an_unparseable_github_payload(self):
        """A record with no readable claim and an unreadable linkage is left
        alone: its `repo`/`comment_ids` are still data, and a legacy read
        would surface them."""
        path = record_path(self.queue, "t1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1,
                                    "github": {"repo": "o/r",
                                               "comment_ids": {"h1": 5}},
                                    "claim": None}))

        self.assertTrue(clear_claim(self.queue, "t1"))

        payload = json.loads(path.read_text())
        self.assertEqual(payload["github"]["comment_ids"], {"h1": 5})

    def test_clear_claim_never_raises(self):
        # A legacy claim to clear, but `.meta` occupied by a regular file:
        # the record cannot be rewritten — reported, not raised.
        (self.queue / "claimed").mkdir()
        seed_legacy_claim(self.queue / "claimed" / "t1.md", owner="inv")
        (self.queue / META_DIR_NAME).write_text("occupied")
        self.assertFalse(clear_claim(self.queue, "t1"))


class AtomicWriteTest(unittest.TestCase):
    """FR-D1/D2."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_no_temp_litter_after_write(self):
        set_claim(self.queue, "t1", owner="inv")
        leftovers = [p for p in (self.queue / META_DIR_NAME).iterdir()
                     if p.suffix == ".tmp" or ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_failed_record_write_raises_claim_metadata_error(self):
        """.meta occupied by a file: the write fails, the claim fails."""
        (self.queue / META_DIR_NAME).write_text("occupied")
        with self.assertRaises(ClaimMetadataError):
            set_claim(self.queue, "t1", owner="inv")

    def test_failed_write_leaves_no_owned_record(self):
        """§5.1 posture: a failed claim write owns nothing anywhere."""
        set_claim(self.queue, "t1", owner="inv-old", claimed_at=1.0)
        # Break os.replace so the second claim write cannot land.
        with mock.patch("harness.core.task_record.os.replace",
                        side_effect=OSError("disk gone")):
            with self.assertRaises(ClaimMetadataError):
                set_claim(self.queue, "t1", owner="inv-new")
        record = read_record(self.queue, "t1")
        self.assertEqual(record.claim.owner, "inv-old")  # old record intact
        leftovers = [p for p in (self.queue / META_DIR_NAME).iterdir()
                     if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class LegacyMigrationTest(unittest.TestCase):
    """FR-E1/E2, §5.5, §5.6."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.queue = Path(self._tmp.name)
        for location in ("pending", "claimed", "active", "review",
                         "parked", "failed", "done"):
            (self.queue / location).mkdir()
        self.addCleanup(self._tmp.cleanup)

    def test_legacy_sidecars_merge_into_one_record(self):
        gh = seed_legacy_gh(self.queue / "pending" / "t1.md",
                            comment_ids={"e": "c"}, demo=True)
        claim = seed_legacy_claim(self.queue / "claimed" / "t1.md",
                                  owner="inv-3", claimed_at=77.0)

        record = read_record(self.queue, "t1")
        self.assertEqual(record.github.issue, 7)
        self.assertEqual(record.github.comment_ids, {"e": "c"})
        self.assertTrue(record.github.demo)
        self.assertEqual(record.claim.owner, "inv-3")
        self.assertEqual(record.claim.claimed_at, 77.0)
        # Legacy files removed only after the durable new write — here it
        # succeeded, so both concerns' files are gone and the record holds
        # everything (FR-A1, FR-E3).
        self.assertFalse(claim.exists())
        self.assertFalse(gh.exists())
        self.assertTrue(record_path(self.queue, "t1").is_file())
        payload = json.loads(record_path(self.queue, "t1").read_text())
        self.assertEqual(payload["github"]["issue"], 7)
        self.assertEqual(payload["github"]["comment_ids"], {"e": "c"})
        self.assertIs(payload["github"]["demo"], True)
        self.assertEqual(payload["claim"]["owner"], "inv-3")

    def test_legacy_task_dir_gh_json_migrates(self):
        sidecar = seed_legacy_dir_gh(self.queue, "done", "t1", issue=11)
        record = read_record(self.queue, "t1")
        self.assertEqual(record.github.issue, 11)
        self.assertFalse(sidecar.exists(),
                         "the task dir's linkage was not adopted")
        self.assertEqual(read_linkage(self.queue, "t1").issue, 11)

    def test_corrupt_new_record_repaired_from_legacy(self):
        """§5.5: legacy read applies, then the concern's write repairs."""
        legacy = seed_legacy_gh(self.queue / "pending" / "t1.md", issue=12)
        path = record_path(self.queue, "t1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ broken")

        record = read_record(self.queue, "t1")
        self.assertEqual(record.github.issue, 12)   # legacy honored

        write_linkage(self.queue, "t1", SyncLinkage(issue=12, repo="o/r"))
        payload = json.loads(path.read_text())      # repaired
        self.assertEqual(payload["github"]["issue"], 12)
        self.assertFalse(legacy.exists())

    def test_new_record_wins_over_disagreeing_legacy(self):
        """§5.6."""
        seed_legacy_gh(self.queue / "pending" / "t1.md", issue=1)
        set_claim(self.queue, "t1", owner="inv")  # writes new record, no gh
        # A legacy gh sidecar appears that disagrees with nothing yet;
        # write a new github section, then plant a disagreeing legacy file.
        write_linkage(self.queue, "t1", SyncLinkage(issue=2, repo="o/r"))
        seed_legacy_gh(self.queue / "pending" / "t1.md", issue=999)

        record = read_record(self.queue, "t1")
        self.assertEqual(record.github.issue, 2)
        self.assertEqual(record.claim.owner, "inv")

    def test_legacy_files_survive_a_failed_migration_write(self):
        """FR-E2: removal only after the new record is durable."""
        claim = seed_legacy_claim(self.queue / "claimed" / "t1.md",
                                  owner="inv-13")
        with mock.patch("harness.core.task_record._write_record",
                        return_value=False):
            record = read_record(self.queue, "t1")
        self.assertEqual(record.claim.owner, "inv-13")  # legacy read applies
        self.assertTrue(claim.exists())                 # nothing durable

        record = read_record(self.queue, "t1")          # next sight repairs
        self.assertEqual(record.claim.owner, "inv-13")
        self.assertFalse(claim.exists())

    def test_migration_is_idempotent(self):
        seed_legacy_claim(self.queue / "claimed" / "t1.md", owner="inv-14")
        first = read_record(self.queue, "t1")
        second = read_record(self.queue, "t1")
        self.assertEqual(first.claim.owner, second.claim.owner)
        self.assertEqual(list((self.queue / META_DIR_NAME).iterdir()),
                         [record_path(self.queue, "t1")])

    def test_legacy_free_queue_is_untouched(self):
        """FR-E4: a read with no metadata anywhere creates nothing."""
        record = read_record(self.queue, "t1")
        self.assertIsNone(record.github)
        self.assertIsNone(record.claim)
        self.assertFalse((self.queue / META_DIR_NAME).exists())

    def test_migrate_legacy_returns_none_when_nothing_to_migrate(self):
        self.assertIsNone(task_record._migrate_legacy(self.queue, "t1"))

    def test_corrupt_legacy_sidecars_do_not_raise(self):
        (self.queue / "pending" / "t1.md.gh.json").write_text("nope")
        (self.queue / "claimed" / "t1.md.claim.json").write_text("[[")
        record = read_record(self.queue, "t1")
        self.assertIsNone(record.github)
        self.assertIsNone(record.claim)

    def test_unrelated_legacy_files_are_left_alone(self):
        other = seed_legacy_gh(self.queue / "pending" / "other.md", issue=21)
        read_record(self.queue, "t1")
        self.assertTrue(other.exists())
        self.assertFalse((self.queue / META_DIR_NAME).exists())


if __name__ == "__main__":
    unittest.main()
