"""T09 — the directory provider owns its own claim lifecycle.

`fetch_pending(claim=True)` moved *every* pending file into `claimed/`, and the
only recovery path (`handlers._requeue_claimed`) lived outside the provider and
matched by slug. The provider now exposes the claims it holds — `list_claims`,
`requeue_claim`, `requeue_all_claims`, `claim_age_hours` — and `limit` lets a
caller claim exactly the number of tasks it asked for.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.providers import (  # noqa: E402
    DirectoryTaskProvider,
    Task,
    TaskProvider,
)


class DirectoryClaimApiTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t09-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.provider = DirectoryTaskProvider(self.pending, self.claimed)

    def _pending_names(self):
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self):
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def test_limit_claims_only_the_first_n(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        tasks = self.provider.fetch_pending(claim=True, limit=1)
        self.assertEqual([t.id for t in tasks], ["001-a"])
        self.assertEqual(self._claimed_names(), ["001-a.md"], "limit over-claimed")
        self.assertEqual(self._pending_names(), ["002-b.md"])
        self.assertEqual(tasks[0].source, "directory:001-a.md")

    def test_a_lost_race_for_the_first_file_does_not_eat_a_limit_slot(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        real_rename = Path.rename

        def stolen(path, target):          # another run got 001-a.md first
            if path.name == "001-a.md":
                raise OSError("already claimed")
            return real_rename(path, target)

        with mock.patch.object(Path, "rename", stolen):
            tasks = self.provider.fetch_pending(claim=True, limit=1)
        self.assertEqual([t.id for t in tasks], ["002-b"])  # lost claim not counted
        self.assertEqual(self._claimed_names(), ["002-b.md"])
        self.assertEqual(self._pending_names(), ["001-a.md"])

    def test_no_limit_still_claims_everything(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        tasks = self.provider.fetch_pending(claim=True)
        self.assertEqual([t.id for t in tasks], ["001-a", "002-b"])
        self.assertEqual(self._pending_names(), [])
        self.assertEqual(self._claimed_names(), ["001-a.md", "002-b.md"])

    def test_limit_without_claim_lists_a_prefix_and_claims_nothing(self):
        (self.pending / "001-a.md").write_text("A")
        (self.pending / "002-b.md").write_text("B")
        self.assertEqual([t.id for t in self.provider.fetch_pending(limit=1)], ["001-a"])
        self.assertEqual(len(self.provider.fetch_pending()), 2)
        self.assertEqual(self._pending_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self._claimed_names(), [])

    def test_list_claims_reports_claimed_files_in_sorted_order(self):
        (self.claimed / "002-b.md").write_text("B")
        (self.claimed / "001-a.md").write_text("A")
        claims = self.provider.list_claims()
        self.assertEqual([c.source for c in claims],
                         ["claimed:001-a.md", "claimed:002-b.md"])
        self.assertEqual([c.id for c in claims], ["001-a", "002-b"])
        self.assertEqual([c.body for c in claims], ["A", "B"])

    def test_requeue_claim_by_task(self):
        (self.pending / "001-a.md").write_text("A")
        task = self.provider.fetch_pending(claim=True, limit=1)[0]
        dest = self.provider.requeue_claim(task)
        self.assertEqual(dest, str(self.pending / "001-a.md"))
        self.assertTrue((self.pending / "001-a.md").exists())
        self.assertEqual(self._claimed_names(), [])

    def test_requeue_claim_by_filename_and_bare_stem(self):
        (self.claimed / "004-d.md").write_text("D")
        self.assertEqual(self.provider.requeue_claim("004-d.md"),
                         str(self.pending / "004-d.md"))
        (self.claimed / "005-e.md").write_text("E")
        self.assertEqual(self.provider.requeue_claim("005-e"),
                         str(self.pending / "005-e.md"))

    def test_requeue_claim_never_overwrites_a_pending_file(self):
        (self.pending / "001-a.md").write_text("original")
        (self.claimed / "001-a.md").write_text("claimed copy")
        dest = self.provider.requeue_claim("001-a.md")
        self.assertEqual(dest, str(self.pending / "001-a-requeued.md"))
        self.assertEqual((self.pending / "001-a.md").read_text(), "original")
        self.assertEqual((self.pending / "001-a-requeued.md").read_text(),
                         "claimed copy")

    def test_requeue_claim_returns_none_when_absent(self):
        self.assertIsNone(self.provider.requeue_claim("nope.md"))
        self.assertIsNone(self.provider.requeue_claim(Task(id="nope", body="")))
        self.assertEqual(self._pending_names(), [])

    def test_requeue_all_claims_moves_everything_and_reports_names(self):
        (self.claimed / "004-d.md").write_text("D")
        (self.claimed / "003-c.md").write_text("C")
        self.assertEqual(self.provider.requeue_all_claims(), ["003-c.md", "004-d.md"])
        self.assertEqual(self._pending_names(), ["003-c.md", "004-d.md"])
        self.assertEqual(self._claimed_names(), [])

    def test_requeue_all_claims_on_an_empty_queue(self):
        self.assertEqual(self.provider.requeue_all_claims(), [])

    def test_claim_age_hours(self):
        f = self.claimed / "004-d.md"
        f.write_text("D")
        self.assertGreaterEqual(self.provider.claim_age_hours("004-d.md"), 0)
        stamp = f.stat().st_mtime - 7200
        os.utime(f, (stamp, stamp))
        self.assertAlmostEqual(self.provider.claim_age_hours("004-d.md"), 2.0, delta=0.1)
        self.assertEqual(self.provider.claim_age_hours("nope.md"), -1.0)

    def test_claim_age_hours_accepts_a_slugified_task_id(self):
        """A caller (status, T12's requeue-claims) only has `Task.id`, which is
        slugified and truncated at 60 chars — it must still find the claim."""
        f = self.claimed / ("007-" + "x" * 80 + ".md")
        f.write_text("L")
        claim = self.provider.list_claims()[0]
        self.assertNotEqual(claim.id, f.stem, "expected a truncated id")
        stamp = f.stat().st_mtime - 3600
        os.utime(f, (stamp, stamp))
        self.assertAlmostEqual(self.provider.claim_age_hours(claim.id), 1.0, delta=0.1)

    def test_failed_move_leaves_the_claim_intact(self):
        (self.claimed / "001-a.md").write_text("A")
        with mock.patch.object(Path, "rename", side_effect=OSError("busy")):
            self.assertIsNone(self.provider.requeue_claim("001-a.md"))
            self.assertEqual(self.provider.requeue_all_claims(), [])
        self.assertEqual(self._claimed_names(), ["001-a.md"])


class NonDirectoryProviderDefaultsTest(unittest.TestCase):
    """A source with no claim concept stays a valid adapter."""

    class NullProvider(TaskProvider):
        def fetch_pending(self) -> list[Task]:
            return []

    def test_claim_api_defaults(self):
        provider = self.NullProvider()
        self.assertEqual(provider.list_claims(), [])
        self.assertEqual(provider.requeue_all_claims(), [])
        self.assertIsNone(provider.requeue_claim(Task(id="x", body="")))
        self.assertEqual(provider.claim_age_hours("x.md"), -1.0)


if __name__ == "__main__":
    unittest.main()
