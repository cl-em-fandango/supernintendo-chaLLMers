"""T51 — a claim records who holds it, and only that owner may hand it back.

Claims were identified by filename and age alone, so a cleanup path could not
tell its own claim from one held by another live invocation and handing back
"the leftovers" could steal a running peer's work. The directory provider now
takes an optional owner id at claim time and writes an adjacent JSON sidecar
(`claim_metadata.py`) recording it; a requeue that names an owner moves only
claims recorded against that owner.

Covered here:
  * the sidecar itself — atomic write, unknown on absent/corrupt reads;
  * claim rename + metadata creation is rollback-safe: a sidecar that cannot be
    written puts the markdown back in pending/ and raises;
  * two owners — owner A cannot requeue owner B's claim, by Task or by name,
    one-at-a-time or in bulk;
  * claims with missing or corrupt metadata read as `owner=unknown` and are
    refused an ownership-checked requeue rather than silently stolen;
  * an owner-less requeue keeps the pre-ownership behaviour (the CLI is not
    generating owner ids yet — that is T52).
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core import providers  # noqa: E402
from harness.core.claim_metadata import (  # noqa: E402
    OWNER_UNKNOWN,
    ClaimMetadataError,
    metadata_path,
    read_metadata,
    write_metadata,
)
from harness.core.providers import (  # noqa: E402
    Claim,
    DirectoryTaskProvider,
    Task,
    TaskProvider,
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

    def _sidecar_names(self):
        return sorted(p.name for p in self.claimed.glob("*.claim.json"))

    def _logged(self):
        return " | ".join(self.messages)

    def _claim_one(self, name: str, body: str, owner: str) -> Task:
        (self.pending / name).write_text(body)
        return self.provider.fetch_pending(claim=True, limit=1, owner=owner)[0]


class SidecarFormatTest(_QueueFixture):
    """The metadata file itself: where it lives, what it holds, what it tolerates."""

    def test_claim_writes_the_sidecar_beside_the_markdown(self):
        self._claim_one("001-a.md", "A", "run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._sidecar_names(), ["001-a.md.claim.json"])

        sidecar = self.claimed / "001-a.md.claim.json"
        self.assertEqual(sidecar, metadata_path(self.claimed / "001-a.md"))
        payload = json.loads(sidecar.read_text())
        self.assertEqual(payload["owner"], "run-a")
        self.assertEqual(payload["claim_file"], "001-a.md")
        self.assertEqual(payload["version"], 1)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        claim_file = self.claimed / "001-a.md"
        claim_file.write_text("A")
        before = time.time()
        dest = write_metadata(claim_file, "run-a")
        self.assertEqual(dest, metadata_path(claim_file))
        self.assertEqual(sorted(p.name for p in self.claimed.glob("*.tmp*")), [])
        self.assertEqual(sorted(p.name for p in self.claimed.glob("*.tmp")), [])
        self.assertGreaterEqual(read_metadata(claim_file).claimed_at, before - 1)

    def test_written_owner_and_time_read_back(self):
        claim_file = self.claimed / "001-a.md"
        claim_file.write_text("A")
        write_metadata(claim_file, "run-a", claimed_at=1234.5)
        meta = read_metadata(claim_file)
        self.assertEqual((meta.owner, meta.claimed_at), ("run-a", 1234.5))
        self.assertTrue(meta.is_known)

    def test_absent_sidecar_reads_unknown(self):
        meta = read_metadata(self.claimed / "nope.md")
        self.assertEqual(meta.owner, OWNER_UNKNOWN)
        self.assertEqual(meta.claimed_at, 0.0)
        self.assertFalse(meta.is_known)

    def test_corrupt_sidecars_read_unknown(self):
        cases = {
            "not json at all": "owner = run-a",
            "json but not an object": '["run-a"]',
            "no owner key": '{"claimed_at": 1.0}',
            "blank owner": '{"owner": ""}',
            "non-string owner": '{"owner": 7}',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                claim_file = self.claimed / "001-a.md"
                claim_file.write_text("A")
                metadata_path(claim_file).write_text(text)
                self.assertEqual(read_metadata(claim_file).owner, OWNER_UNKNOWN)

    def test_a_bad_timestamp_does_not_make_the_owner_unknown(self):
        claim_file = self.claimed / "001-a.md"
        claim_file.write_text("A")
        metadata_path(claim_file).write_text('{"owner": "run-a", "claimed_at": "soon"}')
        meta = read_metadata(claim_file)
        self.assertEqual(meta.owner, "run-a")
        self.assertEqual(meta.claimed_at, 0.0)


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
        self.assertEqual(claim.meta_path, self.claimed / "001-a.md.claim.json")

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
        self.assertEqual(self._sidecar_names(), ["002-b.md.claim.json"])

    def test_requeue_without_an_owner_still_moves_any_claim(self):
        """The pre-ownership call is unchecked; the CLI only names an owner
        once T52 wires owner ids through the run commands."""
        self._claim_one("001-a.md", "A", "run-a")
        self.assertEqual(self.provider.requeue_claim("001-a.md"),
                         str(self.pending / "001-a.md"))
        self.assertEqual(self._claimed_names(), [])

    def test_requeued_claim_leaves_no_sidecar_behind(self):
        self._claim_one("001-a.md", "A", "run-a")
        self.provider.requeue_claim("001-a.md", owner="run-a")
        self.assertEqual(self._sidecar_names(), [])
        self.assertEqual(read_metadata(self.pending / "001-a.md").owner,
                         OWNER_UNKNOWN)

    def test_claim_without_metadata_is_unknown_and_refused(self):
        (self.claimed / "001-a.md").write_text("A")
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner="run-a"))
        self.assertEqual(self.provider.requeue_all_claims(owner="run-a"), [])
        self.assertEqual(self._claimed_names(), ["001-a.md"])

    def test_corrupt_metadata_is_unknown_and_refused(self):
        (self.claimed / "001-a.md").write_text("A")
        metadata_path(self.claimed / "001-a.md").write_text("{ not json")
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)
        self.assertIsNone(self.provider.requeue_claim("001-a.md", owner="run-a"))
        self.assertEqual(self._claimed_names(), ["001-a.md"])

    def test_release_claim_removes_the_sidecar(self):
        task = self._claim_one("001-a.md", "A", "run-a")
        self.provider.release_claim(task)
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._sidecar_names(), [])


class ClaimMetadataRollbackTest(_QueueFixture):
    """A claim without an owner is worse than no claim: it rolls back."""

    def test_a_sidecar_write_failure_raises_a_named_error(self):
        claim_file = self.claimed / "001-a.md"
        claim_file.write_text("A")
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                write_metadata(claim_file, "run-a")
        self.assertEqual(sorted(p.name for p in self.claimed.glob("*.tmp*")), [])

    def test_failed_metadata_write_returns_the_markdown_to_pending(self):
        (self.pending / "001-a.md").write_text("original body")
        with mock.patch.object(providers, "write_metadata",
                               side_effect=ClaimMetadataError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), [], "claim survived its rollback")
        self.assertEqual(self._sidecar_names(), [])
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual((self.pending / "001-a.md").read_text(), "original body")

    def test_a_real_write_failure_rolls_the_claim_back(self):
        """Same path, driven by the filesystem rather than a patched provider call."""
        (self.pending / "001-a.md").write_text("original body")
        with mock.patch.object(Path, "write_text", side_effect=OSError("disk full")):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._pending_names(), ["001-a.md"])
        self.assertEqual(self._claimed_names(), [])
        self.assertEqual(self._sidecar_names(), [])

    def test_only_the_claim_that_failed_is_rolled_back(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        real_write = providers.write_metadata
        calls = []

        def flaky(claim_file, owner, claimed_at=None):
            calls.append(claim_file.name)
            if len(calls) == 2:
                raise ClaimMetadataError("disk full")
            return real_write(claim_file, owner, claimed_at)

        with mock.patch.object(providers, "write_metadata", side_effect=flaky):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._sidecar_names(), ["001-a.md.claim.json"])
        self.assertEqual(self._pending_names(), ["002-b.md"])

    def test_a_rollback_that_cannot_move_back_is_logged_and_still_raises(self):
        (self.pending / "001-a.md").write_text("A")
        with mock.patch.object(providers, "write_metadata",
                               side_effect=ClaimMetadataError("disk full")), \
                mock.patch.object(self.provider, "_move_to_pending",
                                  return_value=None):
            with self.assertRaises(ClaimMetadataError):
                self.provider.fetch_pending(claim=True, owner="run-a")
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertIn("could not be rolled back", self._logged())

    def test_claiming_without_an_owner_writes_no_sidecar(self):
        (self.pending / "001-a.md").write_text("A")
        self.provider.fetch_pending(claim=True)
        self.assertEqual(self._claimed_names(), ["001-a.md"])
        self.assertEqual(self._sidecar_names(), [])
        self.assertEqual(self.provider.list_owned_claims()[0].owner, OWNER_UNKNOWN)


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
