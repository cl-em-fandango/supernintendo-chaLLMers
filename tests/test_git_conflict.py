"""T72 — a failed squash merge is cleaned up without a broad untracked sweep.

`git merge --squash` never writes `MERGE_HEAD`, so on a conflict the repo is left
with unmerged index entries plus whatever the squash staged, and a follow-up
`git merge --abort` exits 128 ("There is no merge to abort (MERGE_HEAD
missing)"). The cleanup therefore cannot rely on `merge --abort`: it must be
`git reset -q` (clears the conflict stages) + `git checkout -q -- .` (restores
the worktree), plus removal of *exactly* the paths the branch added — never a
`git status --porcelain` `??` sweep, because a concurrent tool may have dropped
an unrelated file in the worktree after `_require_clean` proved it clean.

Out of scope here (T73 and friends own them): the squash *commit* failing, the
verification gate, the revert path and branch deletion.

Run from the repo root:  python3 -m unittest tests.test_git_conflict
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external import git_cli as G  # noqa: E402


class SquashConflictCleanupTest(unittest.TestCase):
    """One fixture class: trunk, a branch that edits f.txt and adds paths, a
    conflicting trunk commit, and a scratch directory outside the worktree."""

    TRUNK = "pi/trunk"
    BRANCH = "pi/conflict"
    ADDED = {"added_by_feature.txt", "added_dir/nested.txt", "branch_link"}

    def setUp(self):
        # one common parent so `../outside/<file>` is a *real* escape from the
        # worktree rather than a path to nowhere
        self.base = Path(tempfile.mkdtemp(prefix="t72-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.dir = self.base / "repo"
        self.dir.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

        self._git("init", "-b", self.TRUNK)
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("base")

        self._git("checkout", "-b", self.BRANCH)
        self._write("f.txt", "feature\n")
        self._write("added_by_feature.txt", "new file from the branch\n")
        self._write("added_dir/nested.txt", "nested file from the branch\n")
        (self.dir / "branch_link").symlink_to(self.outside / "precious.txt")
        self._git("add", "-A")
        self._commit("feat")

        self._git("checkout", self.TRUNK)
        self._write("f.txt", "trunk\n")
        self._git("add", "-A")
        self._commit("trunk-change")
        self.trunk_sha = self._git("rev-parse", "HEAD").strip()
        (self.outside / "precious.txt").write_text("someone else's file\n")

    # -- helpers ----------------------------------------------------------
    def _write(self, rel: str, text: str) -> None:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def _git(self, *args: str) -> str:
        r = subprocess.run(["git", *args], cwd=self.dir,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, (args, r.stderr))
        return r.stdout

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        """Run git without asserting on the return code (conflicts are expected)."""
        return subprocess.run(["git", *args], cwd=self.dir,
                              capture_output=True, text=True)

    def _commit(self, msg: str) -> None:
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)

    def porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def staged(self) -> str:
        return self._git("diff", "--cached", "--name-only").strip()

    def unmerged(self) -> str:
        return self._git("ls-files", "-u").strip()

    def read(self, rel: str) -> str:
        return (self.dir / rel).read_text()

    def squash_raw(self) -> subprocess.CompletedProcess:
        """The bare `git merge --squash`, so the wreck can be inspected directly."""
        return self._run("merge", "--squash", self.BRANCH)

    def abort_via_merge_to_trunk(self) -> str:
        with self.assertRaises(RuntimeError) as ctx:
            G.merge_to_trunk(self.dir, "conflict", self.TRUNK, "title")
        return str(ctx.exception)

    # -- merge_in_progress -------------------------------------------------
    def test_clean_repo_is_not_mid_merge(self):
        self.assertFalse(G.merge_in_progress(self.dir))

    def test_squash_conflict_is_mid_merge_without_merge_head(self):
        """The premise of the whole card: `MERGE_HEAD` alone reports a wrecked
        squash as clean, and `merge --abort` is a no-op that fails."""
        squash = self.squash_raw()
        self.assertNotEqual(squash.returncode, 0, "fixture no longer conflicts")
        self.assertFalse((G._gitdir(self.dir) / "MERGE_HEAD").exists(),
                         "a squash must not be assumed to write MERGE_HEAD")
        self.assertTrue(self.unmerged(), "no unmerged index entries were created")
        self.assertTrue(G.merge_in_progress(self.dir),
                        "unmerged index entries alone must count as mid-merge")
        abort = self._run("merge", "--abort")
        self.assertNotEqual(abort.returncode, 0,
                            "merge --abort unexpectedly worked — the premise changed")

    def test_plain_merge_conflict_reports_mid_merge(self):
        self.assertNotEqual(self._run("merge", self.BRANCH).returncode, 0)
        self.assertTrue((G._gitdir(self.dir) / "MERGE_HEAD").exists())
        self.assertTrue(G.merge_in_progress(self.dir))

    # -- added-path recording ---------------------------------------------
    def test_added_paths_are_recorded_before_the_merge(self):
        """Only branch additions, captured while the tree is still untouched —
        the list is the allow-list the cleanup deletes from."""
        real = G._added_paths
        seen: dict[str, object] = {}

        def spy(workdir, trunk, branch):
            seen["added"] = real(workdir, trunk, branch)
            seen["porcelain"] = self.porcelain()
            seen["unmerged"] = self.unmerged()
            return seen["added"]

        with mock.patch.object(G, "_added_paths", side_effect=spy):
            msg = self.abort_via_merge_to_trunk()

        self.assertIn("merge conflict", msg)
        self.assertEqual(set(seen["added"]), self.ADDED)
        self.assertEqual(seen["porcelain"], "", "recorded after the tree was already touched")
        self.assertEqual(seen["unmerged"], "", "recorded after the merge started")

    # -- conflict cleanup through merge_to_trunk ---------------------------
    def test_squash_conflict_raises_with_evidence(self):
        msg = self.abort_via_merge_to_trunk()
        self.assertIn("merge conflict", msg)
        self.assertIn("CONFLICT", msg, "git conflict output missing from the message")
        self.assertIn(self.trunk_sha[:8], msg, "pre-merge trunk sha missing")

    def test_squash_conflict_leaves_head_index_and_worktree_clean(self):
        self.abort_via_merge_to_trunk()
        self.assertEqual(self.unmerged(), "", "unmerged index entries left behind")
        self.assertEqual(self.staged(), "", "index left staged")
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")
        self.assertFalse(G.merge_in_progress(self.dir))
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.trunk_sha,
                         "HEAD moved on a failed merge")
        self.assertEqual(self.read("f.txt"), "trunk\n", "worktree not restored")
        self.assertNotIn("<<<<<<<", self.read("f.txt"), "conflict markers left behind")
        for rel in self.ADDED:
            self.assertFalse((self.dir / rel).exists(),
                             f"branch-added path survived cleanup: {rel}")
        self.assertFalse((self.dir / "added_dir").exists(),
                         "directory emptied by the cleanup was not pruned")

    def test_unrelated_untracked_file_survives_the_cleanup(self):
        """A file created *after* the cleanliness check is not ours to delete:
        the cleanup removes recorded paths only, never every `??` entry."""
        bystander = self.dir / "written_by_another_tool.txt"
        real = G._require_clean

        def concurrent_writer_arrives(workdir, what):
            real(workdir, what)                      # the tree *is* clean here...
            bystander.write_text("not ours\n")       # ...and this appears right after

        with mock.patch.object(G, "_require_clean",
                               side_effect=concurrent_writer_arrives):
            msg = self.abort_via_merge_to_trunk()

        self.assertIn("merge conflict", msg)
        self.assertTrue(bystander.exists(), "cleanup deleted a file it never recorded")
        self.assertEqual(bystander.read_text(), "not ours\n")
        self.assertEqual(self.unmerged(), "")
        self.assertEqual(self.staged(), "")
        self.assertEqual(self.porcelain(), f"?? {bystander.name}")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.trunk_sha)

    # -- abort_merge on each merge shape -----------------------------------
    def test_abort_merge_clears_squash_residue_without_merge_head(self):
        self.squash_raw()
        self.assertTrue(self.unmerged())
        G.abort_merge(self.dir, sorted(self.ADDED))
        self.assertEqual(self.unmerged(), "")
        self.assertEqual(self.porcelain(), "")
        self.assertFalse(G.merge_in_progress(self.dir))
        self.assertEqual(self.read("f.txt"), "trunk\n")

    def test_abort_merge_aborts_a_plain_merge(self):
        self._run("merge", self.BRANCH)
        self.assertTrue((G._gitdir(self.dir) / "MERGE_HEAD").exists())
        G.abort_merge(self.dir)
        self.assertFalse((G._gitdir(self.dir) / "MERGE_HEAD").exists())
        self.assertFalse(G.merge_in_progress(self.dir))
        self.assertEqual(self.porcelain(), "")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.trunk_sha)
        self.assertEqual(self.read("f.txt"), "trunk\n")

    # -- _discard_added safety ---------------------------------------------
    def test_discard_added_rejects_paths_outside_the_worktree(self):
        escape = self.outside / "escape.txt"
        escape.write_text("outside content\n")
        absolute = self.outside / "absolute.txt"
        absolute.write_text("absolute content\n")
        sneaky = self.outside / "sneaky.txt"
        sneaky.write_text("sneaky content\n")

        G._discard_added(self.dir, ["../outside/escape.txt", str(absolute),
                                    "subdir/../../outside/sneaky.txt"])

        self.assertEqual(escape.read_text(), "outside content\n")
        self.assertEqual(absolute.read_text(), "absolute content\n")
        self.assertEqual(sneaky.read_text(), "sneaky content\n")

    def test_discard_added_never_deletes_tracked_files(self):
        G._discard_added(self.dir, ["f.txt"])
        self.assertTrue((self.dir / "f.txt").exists())
        self.assertEqual(self.read("f.txt"), "trunk\n")

    def test_discard_added_removes_a_symlink_without_following_it(self):
        link = self.dir / "branch_link"
        link.symlink_to(self.outside / "precious.txt")
        self.assertTrue(link.is_symlink())

        G._discard_added(self.dir, ["branch_link"])

        self.assertFalse(link.exists() or link.is_symlink(), "symlink itself not removed")
        self.assertEqual((self.outside / "precious.txt").read_text(),
                         "someone else's file\n", "the link target was touched")

    def test_discard_added_prunes_only_directories_it_emptied(self):
        self._write("kept/other.txt", "still needed\n")
        self._git("add", "-A")
        self._commit("keep-me")
        self._write("added_dir/nested.txt", "untracked residue\n")

        G._discard_added(self.dir, ["added_dir/nested.txt", "kept/nested.txt"])

        self.assertFalse((self.dir / "added_dir").exists(),
                         "now-empty parent of a removed file was not pruned")
        self.assertTrue((self.dir / "kept" / "other.txt").exists(),
                        "a directory we did not empty was removed")


if __name__ == "__main__":
    unittest.main()
