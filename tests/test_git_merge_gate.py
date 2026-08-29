"""T63 — temp-repo tests for the gated merge: success, tag advancement,
feature-branch retention/cleanup, and gate-failure rollback.

`merge_to_trunk` refuses any repo `gate_applies` cannot judge (T24), so every
fixture here is a throwaway repo carrying the two *recognition* stubs the
predicate looks for — `harness.py` and `harness/composition.py`. They exist as
files only: the repo is never importable, and `verify_harness` is patched for
the duration of each call so the real gate (which would run this repo's own
`harness.py status`) is never pointed at a scratch tree.

Covered:
  * a successful squash merge lands exactly one commit on trunk (a squash,
    not a merge commit),
  * `pi/last-good` is created/advanced only after the gate passes,
  * the feature branch survives the merge and goes only with `cleanup_branch`,
  * a failed gate rolls trunk back to the tag — or to `HEAD~1` with no tag —
    keeps the branch, leaves the tag where it was, and raises with evidence.

Out of scope (T64 and T65 own them): conflicting merges, dirty-worktree
refusal, gate-not-applicable refusal, and anything run against this repo's
real `.git`.

Run from the repo root:  python3 -m unittest tests.test_git_merge_gate -v
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

TRUNK = "pi/trunk"
TASK = "good"
BRANCH = f"pi/{TASK}"


class GateMergeFixture(unittest.TestCase):
    """Shared temp repo: trunk with one commit, the gate-recognition stubs, and
    a feature branch that edits `f.txt` and adds `feature.txt`.

    Every repo lives under its own `tempfile.mkdtemp()` root; nothing here
    touches `/home/donald/work/harness` or `/home/donald/work/queue`.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t63-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-b", TRUNK)
        # T24's recognition check is two file checks — stub files, never a
        # working harness package (the real gate is patched out below).
        self._write("harness.py", "# gate recognition stub\n")
        self._write("harness/composition.py", "# gate recognition stub\n")
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("base")
        self.base_sha = self.rev(TRUNK)

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

    def _commit(self, msg: str) -> None:
        self._git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", msg)

    def rev(self, ref: str) -> str:
        """The commit `ref` resolves to (tags peeled, branches followed)."""
        return self._git("rev-parse", f"{ref}^{{commit}}").strip()

    def porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def unmerged(self) -> str:
        return self._git("ls-files", "-u").strip()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def make_feature(self, task_id: str = TASK, marker: str = "feature") -> None:
        """Feature branch `pi/<task_id>`: one edit plus one added file.

        `marker` keeps each branch's content distinct — two features that write
        identical bytes leave the second one with nothing to commit."""
        self._git("checkout", "-b", f"pi/{task_id}")
        self._write("f.txt", f"base + {marker}\n")
        self._write("feature.txt", f"work product from {marker}\n")
        self._git("add", "-A")
        self._commit(f"feat:{task_id}")
        self._git("checkout", TRUNK)

    def merge(self, ok: bool = True, detail: str = "ok",
              task_id: str = TASK) -> None:
        """`merge_to_trunk` with the verification gate's answer fixed.

        Patching `verify_harness` (never weakening it) is what lets a scratch
        repo stand in for the harness: the merge logic and the git effects are
        real, only the verdict is supplied.
        """
        with mock.patch.object(G, "verify_harness", return_value=(ok, detail)):
            G.merge_to_trunk(self.dir, task_id, TRUNK, "title")

    def failed_merge(self, detail: str = "import failed: boom") -> str:
        """Run the merge with a failing gate and return the exception message."""
        with self.assertRaises(RuntimeError) as ctx:
            self.merge(ok=False, detail=detail)
        return str(ctx.exception)


# ------------------------------------------------------------------
# successful gated merge
# ------------------------------------------------------------------

class HappyMergeTest(GateMergeFixture):
    def setUp(self):
        super().setUp()
        self.make_feature()
        self.pre_merge_sha = self.rev(TRUNK)
        self.branch_sha = self.rev(BRANCH)

    def test_merge_lands_one_squash_commit(self):
        self.merge()

        self.assertEqual(self._git("rev-list", "--count", TRUNK).strip(), "2",
                         "the merge was not squashed into a single commit")
        parents = self._git("log", "-1", "--format=%P").split()
        self.assertEqual(len(parents), 1,
                         "trunk head has several parents: a merge commit, not a squash")
        self.assertEqual(parents[0], self.pre_merge_sha,
                         "the squash commit was not built on the pre-merge trunk")
        self.assertIn("feat(good): title",
                      self._git("log", "-1", "--format=%s"))
        self.assertIn(f"Squash-merged from {BRANCH}.",
                      self._git("log", "-1", "--format=%b"))
        self.assertEqual(self._git("log", "-1", "--format=%an").strip(),
                         "pi-harness", "squash commit author is not the harness")

    def test_merge_moves_trunk_and_carries_the_work(self):
        self.merge()

        head = self.rev(TRUNK)
        self.assertNotEqual(head, self.pre_merge_sha, "trunk did not move")
        self.assertEqual((self.dir / "f.txt").read_text(), "base + feature\n")
        self.assertEqual((self.dir / "feature.txt").read_text(),
                         "work product from feature\n")
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")
        self.assertEqual(self.unmerged(), "", "unmerged index entries left behind")
        self.assertFalse(G.merge_in_progress(self.dir))
        self.assertEqual(self.current_branch(), TRUNK)

    def test_gate_judges_the_merged_trunk(self):
        """The gate runs *after* the squash commit, on trunk, on a clean tree —
        a gate that ran before the commit would judge nothing."""
        seen: dict[str, str] = {}

        def spy(workdir):
            seen["workdir"] = str(Path(workdir))
            seen["branch"] = self.current_branch()
            seen["subject"] = self._git("log", "-1", "--format=%s").strip()
            seen["porcelain"] = self.porcelain()
            return True, "ok"

        with mock.patch.object(G, "verify_harness", side_effect=spy):
            G.merge_to_trunk(self.dir, TASK, TRUNK, "title")

        self.assertEqual(seen["workdir"], str(self.dir))
        self.assertEqual(seen["branch"], TRUNK)
        self.assertIn("feat(good): title", seen["subject"])
        self.assertEqual(seen["porcelain"], "")

    def test_feature_branch_survives_a_successful_merge(self):
        """F8: deletion is `cleanup_branch`'s job, so a crash between the merge
        and completion still has the branch to resume from."""
        self.merge()

        self.assertTrue(G.has_branch(self.dir, BRANCH),
                        "merge_to_trunk deleted the feature branch")
        self.assertEqual(self.rev(BRANCH), self.branch_sha,
                         "the merge moved the feature branch")


# ------------------------------------------------------------------
# last-good tag advancement
# ------------------------------------------------------------------

class LastGoodTagAdvancementTest(GateMergeFixture):
    def test_first_successful_merge_creates_the_tag(self):
        self.make_feature()
        self.assertFalse(G.has_tag(self.dir, G.LAST_GOOD_TAG))

        self.merge()

        self.assertTrue(G.has_tag(self.dir, G.LAST_GOOD_TAG),
                        "a passing gate did not advance pi/last-good")
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.rev(TRUNK),
                         "pi/last-good does not point at the merged trunk commit")

    def test_tag_moves_from_the_previous_good_commit(self):
        self._git("tag", G.LAST_GOOD_TAG)          # base is the last good commit
        self.make_feature()

        self.merge()

        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.rev(TRUNK))
        self.assertNotEqual(self.rev(G.LAST_GOOD_TAG), self.base_sha,
                            "pi/last-good was left behind at the old commit")

    def test_second_successful_merge_advances_the_tag_again(self):
        self.make_feature("first")
        self.merge(task_id="first")
        first_tag = self.rev(G.LAST_GOOD_TAG)

        self.make_feature("second", marker="second")
        self.merge(task_id="second")

        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.rev(TRUNK))
        self.assertNotEqual(self.rev(G.LAST_GOOD_TAG), first_tag,
                            "the tag did not move on the second merge")

    def test_failed_gate_leaves_the_tag_where_it_was(self):
        self._git("tag", G.LAST_GOOD_TAG)
        self.make_feature()

        self.failed_merge()

        self.assertTrue(G.has_tag(self.dir, G.LAST_GOOD_TAG))
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.base_sha,
                         "a failed gate advanced pi/last-good")


# ------------------------------------------------------------------
# gate failure: rollback
# ------------------------------------------------------------------

class GateFailureRollbackTest(GateMergeFixture):
    def setUp(self):
        super().setUp()
        self.make_feature()
        self.pre_merge_sha = self.rev(TRUNK)
        self.branch_sha = self.rev(BRANCH)

    def test_rollback_to_tag_restores_trunk(self):
        self._git("tag", G.LAST_GOOD_TAG)

        msg = self.failed_merge(detail="import failed: harness.core.gitops")

        self.assertIn("verification gate FAILED", msg)
        self.assertIn("import failed: harness.core.gitops", msg,
                      "the gate's own detail is missing from the message")
        self.assertIn(f"trunk reverted to tag:{G.LAST_GOOD_TAG}", msg,
                      "the message must name the ref actually reverted to")
        self.assertIn(self.pre_merge_sha, msg, "pre-merge trunk sha missing")
        self.assertIn(f"feature branch {BRANCH} kept", msg)

        self.assertEqual(self.rev(TRUNK), self.pre_merge_sha,
                         "trunk was not rolled back to the last-good tag")
        self.assertEqual((self.dir / "f.txt").read_text(), "base\n",
                         "worktree still carries the merged change")
        self.assertFalse((self.dir / "feature.txt").exists(),
                         "the merged file survived the rollback")

    def test_rollback_without_a_tag_falls_back_to_head_parent(self):
        """No tag yet: the only thing to undo is the merge commit itself, and
        the message must say `HEAD~1` rather than claim a tag revert (T03)."""
        msg = self.failed_merge()

        self.assertIn("verification gate FAILED", msg)
        self.assertIn("trunk reverted to HEAD~1", msg)
        self.assertNotIn(f"tag:{G.LAST_GOOD_TAG}", msg)
        self.assertEqual(self.rev(TRUNK), self.base_sha)
        self.assertFalse(G.has_tag(self.dir, G.LAST_GOOD_TAG),
                         "a failed gate created the last-good tag")

    def test_rollback_leaves_no_merge_residue(self):
        """Asserted with `git ls-files -u` and `merge_in_progress`, never with
        `.git/MERGE_HEAD`: `merge --squash` never writes one, so its absence
        is an assertion that cannot fail (T04)."""
        self._git("tag", G.LAST_GOOD_TAG)

        self.failed_merge()

        self.assertEqual(self.unmerged(), "", "unmerged index entries left behind")
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")
        self.assertFalse(G.merge_in_progress(self.dir),
                         "repo left reporting itself mid-merge")

    def test_rollback_keeps_the_feature_branch_intact(self):
        """The branch is the operator's evidence: kept on every failure path,
        at exactly the commit it had before the merge."""
        self._git("tag", G.LAST_GOOD_TAG)

        self.failed_merge()

        self.assertTrue(G.has_branch(self.dir, BRANCH),
                        "the rollback deleted the feature branch")
        self.assertEqual(self.rev(BRANCH), self.branch_sha,
                         "the feature branch moved during the rollback")

    def test_rollback_stops_at_trunk_and_does_not_retry(self):
        """One gate run, one revert: the gate failing must not loop."""
        self._git("tag", G.LAST_GOOD_TAG)
        calls: list[str] = []

        def failing_gate(workdir):
            calls.append(str(Path(workdir)))
            return False, "harness.py status failed rc=1"

        with mock.patch.object(G, "verify_harness", side_effect=failing_gate):
            with self.assertRaises(RuntimeError):
                G.merge_to_trunk(self.dir, TASK, TRUNK, "title")

        self.assertEqual(calls, [str(self.dir)],
                         f"the gate ran {len(calls)} times, expected exactly once")
        self.assertEqual(self.rev(TRUNK), self.pre_merge_sha)


# ------------------------------------------------------------------
# branch cleanup after completion
# ------------------------------------------------------------------

class BranchCleanupTest(GateMergeFixture):
    def test_cleanup_branch_deletes_after_a_successful_merge(self):
        self.make_feature()
        self.merge()
        merged_trunk = self.rev(TRUNK)

        G.cleanup_branch(self.dir, TASK, TRUNK)

        self.assertFalse(G.has_branch(self.dir, BRANCH),
                         "cleanup_branch did not delete the feature branch")
        self.assertEqual(self.rev(TRUNK), merged_trunk,
                         "cleanup moved trunk")
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), merged_trunk,
                         "cleanup disturbed the last-good tag")
        self.assertEqual((self.dir / "feature.txt").read_text(),
                         "work product from feature\n",
                         "the merged work was removed with the branch")
        self.assertEqual(self.porcelain(), "")

    def test_cleanup_branch_from_the_branch_itself_checks_out_trunk(self):
        self.make_feature()
        self.merge()
        self._git("checkout", BRANCH)

        G.cleanup_branch(self.dir, TASK, TRUNK)

        self.assertEqual(self.current_branch(), TRUNK)
        self.assertFalse(G.has_branch(self.dir, BRANCH))
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")

    def test_cleanup_branch_is_idempotent(self):
        self.make_feature()
        self.merge()
        G.cleanup_branch(self.dir, TASK, TRUNK)

        G.cleanup_branch(self.dir, TASK, TRUNK)   # a resumed task cleans up again

        self.assertFalse(G.has_branch(self.dir, BRANCH))
        self.assertEqual(self.porcelain(), "")

    def test_cleanup_branch_without_a_branch_is_a_noop(self):
        G.cleanup_branch(self.dir, "never-created", TRUNK)

        self.assertFalse(G.has_branch(self.dir, "pi/never-created"))
        self.assertEqual(self.rev(TRUNK), self.base_sha)
        self.assertEqual(self.current_branch(), TRUNK)

    def test_merged_then_cleaned_task_leaves_a_last_good_trunk(self):
        """The full happy sequence the pipeline performs: merge -> complete ->
        cleanup. Trunk holds the work, the tag names it, no branch remains."""
        self.make_feature()
        self.merge()
        G.cleanup_branch(self.dir, TASK, TRUNK)

        self.assertTrue(G.has_tag(self.dir, G.LAST_GOOD_TAG))
        self.assertEqual(self.rev(G.LAST_GOOD_TAG), self.rev(TRUNK))
        self.assertFalse(G.has_branch(self.dir, BRANCH))
        self.assertEqual(self._git("rev-list", "--count", TRUNK).strip(), "2")


if __name__ == "__main__":
    unittest.main()
