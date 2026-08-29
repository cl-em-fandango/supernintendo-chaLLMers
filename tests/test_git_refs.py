"""T62 — temp-repo tests for has_tag/has_branch, tag revert, ensure_branch
idempotence, and the queue predicate.

Run from the repo root:  python3 -m unittest tests.test_git_refs -v
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.git_cli import (  # noqa: E402
    LAST_GOOD_TAG,
    ensure_branch,
    has_branch,
    has_tag,
    is_under_queue,
    revert_to_last_good,
)


class GitRefTestBase(unittest.TestCase):
    """Shared temp-repo setup: init a repo on pi/trunk with one commit."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t62-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-b", "pi/trunk")
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("initial")

    def _write(self, rel: str, text: str) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.dir,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout

    def _commit(self, msg: str) -> None:
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)


# ------------------------------------------------------------------
# has_branch / has_tag
# ------------------------------------------------------------------

class HasBranchTest(GitRefTestBase):
    def test_existing_branch_returns_true(self):
        self.assertTrue(has_branch(self.dir, "pi/trunk"))

    def test_missing_branch_returns_false(self):
        self.assertFalse(has_branch(self.dir, "pi/nonexistent"))

    def test_tag_is_not_a_branch(self):
        self._git("tag", "v1.0")
        self.assertFalse(has_branch(self.dir, "v1.0"))


class HasTagTest(GitRefTestBase):
    def test_existing_tag_returns_true(self):
        self._git("tag", "v1.0")
        self.assertTrue(has_tag(self.dir, "v1.0"))

    def test_missing_tag_returns_false(self):
        self.assertFalse(has_tag(self.dir, "v1.0"))

    def test_branch_is_not_a_tag(self):
        self._git("checkout", "-b", "feature-x")
        self.assertFalse(has_tag(self.dir, "feature-x"))


# ------------------------------------------------------------------
# revert_to_last_good
# ------------------------------------------------------------------

class RevertToLastGoodTest(GitRefTestBase):
    def test_revert_to_tag(self):
        # create a tag at the initial commit, then add another commit
        self._git("tag", LAST_GOOD_TAG)
        self._write("f.txt", "second\n")
        self._git("add", "-A")
        self._commit("second")
        head_before = self._git("rev-parse", "HEAD").strip()

        result = revert_to_last_good(self.dir, "pi/trunk")

        self.assertEqual(result, f"tag:{LAST_GOOD_TAG}")
        head_after = self._git("rev-parse", "HEAD").strip()
        self.assertNotEqual(head_after, head_before)
        self.assertEqual((self.dir / "f.txt").read_text(), "base\n")

    def test_revert_without_tag_falls_back_to_head_parent(self):
        # add a second commit with no tag anywhere
        self._write("f.txt", "second\n")
        self._git("add", "-A")
        self._commit("second")

        result = revert_to_last_good(self.dir, "pi/trunk")

        self.assertEqual(result, "HEAD~1")
        self.assertEqual((self.dir / "f.txt").read_text(), "base\n")

    def test_revert_refuses_dirty_worktree(self):
        self._git("tag", LAST_GOOD_TAG)
        self._write("f.txt", "second\n")
        self._git("add", "-A")
        self._commit("second")
        # dirty the worktree
        self._write("f.txt", "uncommitted change\n")

        with self.assertRaises(RuntimeError) as ctx:
            revert_to_last_good(self.dir, "pi/trunk")
        self.assertIn("refusing", str(ctx.exception))
        # HEAD must not have moved
        self.assertEqual((self.dir / "f.txt").read_text(), "uncommitted change\n")


# ------------------------------------------------------------------
# ensure_branch idempotence
# ------------------------------------------------------------------

class EnsureBranchIdempotenceTest(GitRefTestBase):
    def test_ensure_branch_creates_and_returns_branch(self):
        branch = ensure_branch(self.dir, "T99", "pi/trunk")
        self.assertEqual(branch, "pi/T99")
        self.assertTrue(has_branch(self.dir, "pi/T99"))

    def test_ensure_branch_is_idempotent(self):
        first = ensure_branch(self.dir, "T99", "pi/trunk")
        second = ensure_branch(self.dir, "T99", "pi/trunk")
        self.assertEqual(first, second)
        self.assertTrue(has_branch(self.dir, "pi/T99"))

    def test_ensure_branch_third_call_still_idempotent(self):
        ensure_branch(self.dir, "T99", "pi/trunk")
        ensure_branch(self.dir, "T99", "pi/trunk")
        third = ensure_branch(self.dir, "T99", "pi/trunk")
        self.assertEqual(third, "pi/T99")


# ------------------------------------------------------------------
# is_under_queue predicate
# ------------------------------------------------------------------

class IsUnderQueueTest(unittest.TestCase):
    def setUp(self):
        self.queue = Path(tempfile.mkdtemp(prefix="t62q-")) / "queue"
        self.queue.mkdir(parents=True)

    def test_path_inside_queue(self):
        self.assertTrue(is_under_queue(self.queue / "active" / "t1", self.queue))

    def test_queue_itself(self):
        self.assertTrue(is_under_queue(self.queue, self.queue))

    def test_outside_path(self):
        self.assertFalse(is_under_queue(Path("/tmp/unrelated"), self.queue))

    def test_sibling_prefix_not_contained(self):
        sibling = self.queue.parent / "queue-extra"
        sibling.mkdir()
        self.assertFalse(is_under_queue(sibling, self.queue))

    def test_string_arguments_accepted(self):
        self.assertTrue(is_under_queue(str(self.queue / "active" / "t1"),
                                       str(self.queue)))
        self.assertFalse(is_under_queue("/tmp/queue-nope", str(self.queue)))


if __name__ == "__main__":
    unittest.main()
