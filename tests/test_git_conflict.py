"""T72 — a failed squash merge is cleaned up without a broad untracked sweep.

`git merge --squash` never writes `MERGE_HEAD`, so on a conflict the repo is left
with unmerged index entries plus whatever the squash staged, and a follow-up
`git merge --abort` exits 128 ("There is no merge to abort (MERGE_HEAD
missing)"). The cleanup therefore cannot rely on `merge --abort`: it must be
`git reset -q` (clears the conflict stages) + `git checkout -q -- .` (restores
the worktree), plus removal of *exactly* the paths the branch added — never a
`git status --porcelain` `??` sweep, because a concurrent tool may have dropped
an unrelated file in the worktree after `_require_clean` proved it clean.

Out of scope here (T73 owns them): the squash *commit* failing, the verification
gate and branch deletion.

T64 extends this module with `ConflictCleanupAndDirtyRevertTest`: conflict
cleanup asserted through the module's own pieces (recorded added paths,
`abort_merge`) and the T05 guard that stands in front of every `git reset --hard`
on the revert path.

Run from the repo root:  python3 -m unittest tests.test_git_conflict
"""
from __future__ import annotations

import inspect
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
    @mock.patch.object(G, "gate_applies", return_value=True)
    def test_added_paths_are_recorded_before_the_merge(self, _gate):
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
    @mock.patch.object(G, "gate_applies", return_value=True)
    def test_squash_conflict_raises_with_evidence(self, _gate):
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

    @mock.patch.object(G, "gate_applies", return_value=True)
    def test_unrelated_untracked_file_survives_the_cleanup(self, _gate):
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


# ------------------------------------------------------------------
# T64 — conflict cleanup, and the dirty-tree refusal in front of a revert
# ------------------------------------------------------------------

class ConflictCleanupAndDirtyRevertTest(unittest.TestCase):
    """One fixture: a temp repo whose feature branch conflicts with trunk, plus
    a trunk commit standing in for a bad merge with `pi/last-good` behind it.

    The conflict is cleaned up through the module's own pieces — `_added_paths`
    recorded on a clean tree, `abort_merge` after the wreck — rather than through
    `merge_to_trunk`: that entry point refuses any repo without the T24 gate-
    recognition stubs, and gate recognition is out of scope here. The revert
    guard is exercised at `revert_to_last_good`, the supervisor's breaker entry
    point, which performs no gate check of its own.

    Every repo lives under its own `tempfile.mkdtemp()` root; nothing here
    touches this working tree or `/home/donald/work/queue`.
    """

    TRUNK = "pi/trunk"
    BRANCH = "pi/conflict"
    ADDED = {"feature_new.txt", "feature_dir/nested.txt"}
    MARKERS = ("<<<<<<<", ">>>>>>>")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t64-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-b", self.TRUNK)
        self._write("shared.txt", "base\n")
        self._write("trunk_only.txt", "trunk content\n")
        self._git("add", "-A")
        self._commit("base")
        self.base_sha = self.head()

        self._git("checkout", "-b", self.BRANCH)
        self._write("shared.txt", "feature\n")
        self._write("feature_new.txt", "added by the branch\n")
        self._write("feature_dir/nested.txt", "added by the branch, nested\n")
        self._git("add", "-A")
        self._commit("feat")

        self._git("checkout", self.TRUNK)
        self._write("shared.txt", "trunk\n")
        self._git("add", "-A")
        self._commit("trunk-change")
        self.trunk_sha = self.head()

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

    def unmerged(self) -> str:
        return self._git("ls-files", "-u").strip()

    def read(self, rel: str) -> str:
        return (self.dir / rel).read_text()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def rev(self, ref: str) -> str:
        """The commit `ref` resolves to (tags peeled, branches followed)."""
        return self._git("rev-parse", f"{ref}^{{commit}}").strip()

    def wrecked_squash(self) -> list[str]:
        """Reproduce the conflict path with the module's own pieces.

        `_added_paths` is captured while the tree is still clean — it is the
        allow-list the cleanup deletes from — then trunk is checked out and the
        bare `git merge --squash` is run so it fails. Returns the recorded list.
        """
        self.assertEqual(self.porcelain(), "", "fixture started dirty")
        added = G._added_paths(self.dir, self.TRUNK, self.BRANCH)
        self.assertEqual(set(added), self.ADDED)
        self._git("checkout", self.TRUNK)
        squash = self._run("merge", "--squash", self.BRANCH)
        self.assertNotEqual(squash.returncode, 0, "fixture no longer conflicts")
        return added

    def bad_merge_commit(self, tag: bool = True) -> str:
        """A trunk commit standing in for the squash a failed gate would undo.

        `pi/last-good` is placed on the commit *before* it, so a revert has
        somewhere to go; with `tag=False` only the `HEAD~1` fallback exists.
        Returns the sha a revert would move HEAD away from.
        """
        if tag:
            self._git("tag", G.LAST_GOOD_TAG)
        self._write("merged.txt", "bad work that a revert would discard\n")
        self._git("add", "-A")
        self._commit("feat(bad): the merge that failed the gate")
        return self.head()

    def worktree_texts(self) -> dict[str, str]:
        """Every tracked-or-untracked file in the worktree, keyed by rel path."""
        texts: dict[str, str] = {}
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.dir)
            if ".git" in rel.parts:
                continue
            texts[str(rel)] = p.read_text()
        return texts

    # -- the wreck: what the cleanup has to undo --------------------------
    def test_conflict_wreck_carries_unmerged_entries_and_markers(self):
        """Premise for the assertions below: the merge really conflicted, the
        index really holds conflict stages, and the worktree really carries
        markers — otherwise 'cleaned up' and 'no markers' assert nothing."""
        self.wrecked_squash()

        self.assertTrue(self.unmerged(), "no unmerged index entries were created")
        self.assertTrue(G.merge_in_progress(self.dir))
        self.assertIn("<<<<<<<", self.read("shared.txt"),
                      "the fixture left no conflict marker to clean up")

    # -- unmerged-index cleanup -------------------------------------------
    def test_cleanup_clears_every_unmerged_index_entry(self):
        """`git reset -q` is what clears the conflict stages a squash leaves
        behind; `merge --abort` cannot be relied on (it writes no `MERGE_HEAD`)."""
        added = self.wrecked_squash()

        G.abort_merge(self.dir, added)

        self.assertEqual(self.unmerged(), "", "unmerged index entries left behind")
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")
        self.assertFalse(G.merge_in_progress(self.dir),
                         "repo left reporting itself mid-merge")

    # -- known merge-added path removal -----------------------------------
    def test_cleanup_removes_exactly_the_recorded_merge_added_paths(self):
        """The recorded list is the allow-list: everything in it goes, its emptied
        parent goes with it, and nothing that trunk tracks is touched."""
        added = self.wrecked_squash()

        G.abort_merge(self.dir, added)

        for rel in self.ADDED:
            self.assertFalse((self.dir / rel).exists(),
                             f"branch-added path survived cleanup: {rel}")
        self.assertFalse((self.dir / "feature_dir").exists(),
                         "directory emptied by the cleanup was not pruned")
        self.assertEqual(self.read("trunk_only.txt"), "trunk content\n",
                         "a trunk-tracked file was removed by the cleanup")
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")

    # -- no conflict markers ----------------------------------------------
    def test_cleanup_leaves_no_conflict_markers_in_the_worktree(self):
        """Asserted over every file in the tree, not just the conflicted one: a
        marker left in any path is a wreck the next session would inherit."""
        added = self.wrecked_squash()

        G.abort_merge(self.dir, added)

        texts = self.worktree_texts()
        self.assertIn("shared.txt", texts, "the conflicted file vanished entirely")
        for rel, text in texts.items():
            for marker in self.MARKERS:
                self.assertNotIn(marker, text, f"conflict marker left in {rel}")
        self.assertEqual(self.read("shared.txt"), "trunk\n",
                         "worktree not restored to the trunk version")

    # -- unchanged HEAD ----------------------------------------------------
    def test_cleanup_leaves_head_at_the_pre_merge_trunk_commit(self):
        """Cleanup moves the index and the worktree and nothing else: no commit,
        no reset, no branch switch — HEAD is still the trunk commit the merge
        started from, with the same single-parent history."""
        added = self.wrecked_squash()
        commits_before = self._git("rev-list", "--count", "HEAD").strip()

        G.abort_merge(self.dir, added)

        self.assertEqual(self.head(), self.trunk_sha, "HEAD moved during cleanup")
        self.assertEqual(self._git("rev-parse", "--abbrev-ref", "HEAD").strip(),
                         self.TRUNK, "cleanup switched branch")
        self.assertEqual(self._git("rev-list", "--count", "HEAD").strip(),
                         commits_before, "cleanup created or dropped a commit")
        self.assertTrue(G.has_branch(self.dir, self.BRANCH),
                        "cleanup deleted the feature branch")

    # -- dirty revert refusal ---------------------------------------------
    def test_revert_refuses_an_unstaged_modification(self):
        """T05: a rollback moves commits, it is never a licence to discard
        someone's edits. Both facts the operator needs are in the message."""
        bad_head = self.bad_merge_commit()
        self._write("merged.txt", "local work that is not committed\n")

        with self.assertRaises(RuntimeError) as ctx:
            G.revert_to_last_good(self.dir, self.TRUNK)

        msg = str(ctx.exception)
        self.assertIn("refusing", msg)
        self.assertIn("merged.txt", msg, "the refusal does not name the dirty path")
        self.assertIn(f"git -C {self.dir} status", msg,
                      "the refusal does not give the inspection command")
        self.assertEqual(self.head(), bad_head, "HEAD moved on a refused revert")
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.trunk_sha,
                         "pi/last-good moved on a refused revert")
        self.assertEqual(self.read("merged.txt"),
                         "local work that is not committed\n",
                         "the refusal discarded uncommitted work")

    def test_revert_refuses_a_staged_add(self):
        """Staged work is uncommitted work: `git reset --hard` would drop the
        index entry as surely as an unstaged edit."""
        bad_head = self.bad_merge_commit()
        self._write("staged_work.txt", "staged, never committed\n")
        self._git("add", "staged_work.txt")

        with self.assertRaises(RuntimeError):
            G.revert_to_last_good(self.dir, self.TRUNK)

        self.assertEqual(self.head(), bad_head, "HEAD moved on a refused revert")
        self.assertEqual(self.read("staged_work.txt"), "staged, never committed\n",
                         "the refusal discarded staged work")
        self.assertIn("staged_work.txt", self.porcelain(),
                      "the refused tree was left in a different state")

    def test_revert_refuses_an_untracked_file(self):
        """`dirty_paths` counts untracked paths too, so a file a concurrent tool
        dropped in the tree is enough to stop the rollback."""
        bad_head = self.bad_merge_commit()
        self._write("written_by_another_tool.txt", "not ours\n")

        with self.assertRaises(RuntimeError) as ctx:
            G.revert_to_last_good(self.dir, self.TRUNK)

        self.assertIn("written_by_another_tool.txt", str(ctx.exception))
        self.assertEqual(self.head(), bad_head, "HEAD moved on a refused revert")
        self.assertTrue((self.dir / "written_by_another_tool.txt").exists(),
                        "the refusal deleted an untracked file")

    def test_head_parent_fallback_is_refused_on_a_dirty_tree_too(self):
        """Both `reset --hard` branches sit behind the guard. With no tag the
        fallback resets to `HEAD~1`, which on a dirty tree would silently eat
        the wrong commit *and* the local edits."""
        bad_head = self.bad_merge_commit(tag=False)
        self._write("merged.txt", "local work, no tag to roll back to\n")
        self.assertFalse(G.has_tag(self.dir, G.LAST_GOOD_TAG))

        with self.assertRaises(RuntimeError) as ctx:
            G.revert_to_last_good(self.dir, self.TRUNK)

        self.assertIn("refusing", str(ctx.exception))
        self.assertEqual(self.head(), bad_head, "HEAD moved on a refused revert")
        self.assertEqual(self.read("merged.txt"),
                         "local work, no tag to roll back to\n",
                         "the fallback discarded uncommitted work")

    def test_revert_runs_once_the_tree_is_clean_again(self):
        """Control for the four tests above: the same repo, once clean, rolls
        back — so the refusals above are the guard, not a broken fixture."""
        self.bad_merge_commit()
        self._write("merged.txt", "local work\n")
        with self.assertRaises(RuntimeError):
            G.revert_to_last_good(self.dir, self.TRUNK)

        self._git("checkout", "--", "merged.txt")   # the operator cleaned up
        self.assertEqual(self.porcelain(), "", "tree still dirty after the cleanup")
        reverted_to = G.revert_to_last_good(self.dir, self.TRUNK)

        self.assertEqual(reverted_to, f"tag:{G.LAST_GOOD_TAG}")
        self.assertEqual(self.head(), self.trunk_sha, "clean revert did not roll back")

    def test_public_revert_entry_point_exposes_no_dirty_bypass(self):
        """The breaker has no `allow_dirty`: only the human-driven
        `merge_to_trunk` recovery path may waive the guard, and it propagates
        that waiver to the revert."""
        public = inspect.signature(G.revert_to_last_good)
        guarded = inspect.signature(G._revert_to_last_good)

        self.assertNotIn("allow_dirty", public.parameters,
                         "the breaker's entry point must not offer a bypass")
        self.assertIn("allow_dirty", guarded.parameters,
                         "the human recovery path lost its documented waiver")
        self.assertIs(guarded.parameters["allow_dirty"].default, False,
                      "the waiver must default to refusing a dirty tree")


if __name__ == "__main__":
    unittest.main()
