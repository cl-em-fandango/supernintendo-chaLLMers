"""Enqueue guard — a plan parent marked DO NOT EXECUTE is never enqueued.

`plan-2026-08-26/SLICING-MAP.md`: "Parent files remain as requirement archives
and must not be enqueued when marked **DO NOT EXECUTE**." Nothing enforced it,
so dropping `T04-merge-abort.md` into pending/ claimed an epic and burned a
session re-deriving work T72 and T73 already own.

Two fixtures, one boundary: the pure decision (`EnqueueGuardTest`) and the
fetch path that applies it to a real temp queue (`DirectoryParentSkipTest`).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.core.enqueue_guard import check_enqueue  # noqa: E402
from harness.core.providers import DirectoryTaskProvider  # noqa: E402

# The directive exactly as the plan writes it in every parent card (line 3),
# wrapped across two blockquote lines like T04/T41/T42 really are.
PARENT_BODY = """# T04 — Squash failure-cleanup epic (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Execute T72 then T73. This file is
> retained as the parent contract and conflict reproduction.

**Wave 0** · depends: T03, T05 · `[tag]`
"""

TASK_BODY = """# T99 — Add a status line

## Do
Print the pending count. Never run `git push`; do not execute anything that
touches the network.
"""


class EnqueueGuardTest(unittest.TestCase):
    def test_parent_directive_is_refused_and_names_its_leaves(self):
        d = check_enqueue(PARENT_BODY, "T04-merge-abort.md")
        self.assertFalse(d.allowed)
        self.assertEqual(d.leaves, ("T72", "T73"))
        self.assertIn("T72, T73", d.reason)
        self.assertIn("T04-merge-abort.md", d.reason)
        self.assertNotIn("**", d.directive, "markdown emphasis leaked into the reason")

    def test_ordinary_task_is_allowed(self):
        d = check_enqueue(TASK_BODY, "t99.md")
        self.assertTrue(d.allowed)
        self.assertEqual(d.reason, "")
        self.assertEqual(d.leaves, ())

    def test_prose_mention_in_the_body_does_not_refuse(self):
        """The marker only counts as a header directive, not as something the
        requirement happens to say. T99 above already proves the short case;
        this one uses the exact uppercase phrase outside a blockquote."""
        body = "# T99 — Docs\n\nWrite that operators DO NOT EXECUTE the merge by hand.\n"
        self.assertTrue(check_enqueue(body, "t99.md").allowed)

    def test_marker_below_the_header_window_does_not_refuse(self):
        body = "# T99 — Thing\n\n" + "\n".join(f"line {n}" for n in range(20)) \
               + "\n\n> DO NOT EXECUTE this paragraph, it is a quote.\n"
        self.assertTrue(check_enqueue(body, "t99.md").allowed)

    def test_parent_id_is_not_listed_as_its_own_leaf(self):
        body = ("> **DO NOT EXECUTE THIS FILE AS A CARD.** T04 is superseded; "
                "execute T72 then T73.\n")
        self.assertEqual(check_enqueue(body, "T04-merge-abort.md").leaves,
                         ("T72", "T73"))

    def test_ids_are_deduplicated_in_directive_order(self):
        body = ("> **DO NOT EXECUTE THIS FILE AS A CARD.** T51 → T52 and T53; "
                "T51 first, and T53 reuses it.\n")
        self.assertEqual(check_enqueue(body, "T46-claim-ownership.md").leaves,
                         ("T51", "T52", "T53"))

    def test_refusal_without_named_leaves_still_explains_itself(self):
        body = "> **DO NOT EXECUTE THIS FILE AS A CARD.** See the map.\n"
        d = check_enqueue(body, "parent.md")
        self.assertFalse(d.allowed)
        self.assertEqual(d.leaves, ())
        self.assertIn("the leaves it lists", d.reason)


class DirectoryParentSkipTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="enqueue-guard-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.pending = self.dir / "pending"
        self.claimed = self.dir / "claimed"
        self.pending.mkdir()
        self.claimed.mkdir()
        self.lines: list[str] = []
        self.provider = DirectoryTaskProvider(self.pending, self.claimed,
                                              log=self.lines.append)

    def _pending_names(self):
        return sorted(p.name for p in self.pending.glob("*.md"))

    def _claimed_names(self):
        return sorted(p.name for p in self.claimed.glob("*.md"))

    def test_parent_is_skipped_and_stays_untouched_in_pending(self):
        (self.pending / "001-parent.md").write_text(PARENT_BODY)
        (self.pending / "002-leaf.md").write_text(TASK_BODY)

        tasks = self.provider.fetch_pending(claim=True)

        self.assertEqual([t.id for t in tasks], ["002-leaf"])
        self.assertEqual(self._pending_names(), ["001-parent.md"],
                         "refused parent must stay where it is")
        self.assertEqual(self._claimed_names(), ["002-leaf.md"])
        self.assertTrue(any("DO NOT EXECUTE" in line and "T72, T73" in line
                            for line in self.lines),
                        f"skip was not logged with its leaves: {self.lines}")

    def test_a_refused_parent_does_not_eat_a_limit_slot(self):
        (self.pending / "001-parent.md").write_text(PARENT_BODY)
        (self.pending / "002-leaf.md").write_text(TASK_BODY)

        tasks = self.provider.fetch_pending(claim=True, limit=1)

        self.assertEqual([t.id for t in tasks], ["002-leaf"])
        self.assertEqual(self._claimed_names(), ["002-leaf.md"])

    def test_a_queue_of_only_parents_claims_nothing(self):
        (self.pending / "001-parent.md").write_text(PARENT_BODY)
        self.assertEqual(self.provider.fetch_pending(claim=True), [])
        self.assertEqual(self._claimed_names(), [])

    def test_unmarked_queue_is_unchanged_behaviour(self):
        """Regression: the guard must not disturb a normal queue."""
        (self.pending / "001-a.md").write_text(TASK_BODY)
        (self.pending / "002-b.md").write_text(TASK_BODY)
        tasks = self.provider.fetch_pending(claim=True)
        self.assertEqual([t.id for t in tasks], ["001-a", "002-b"])
        self.assertEqual(self._claimed_names(), ["001-a.md", "002-b.md"])
        self.assertEqual(self.lines, [])


if __name__ == "__main__":
    unittest.main()
