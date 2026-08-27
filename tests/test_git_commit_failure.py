"""T73 — the squash *commit* failing is cleaned up, and evidence survives cleanup failure.

`git merge --squash` can succeed and the following `git commit` still fail (hook,
index/permissions, git config). That path must route through the same
`abort_merge` cleanup as a conflict, report the git stderr tail plus the trunk sha
taken before the merge, and — when the cleanup itself does not get the tree back
to clean — stop and leave the residue for a human instead of resetting further.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external import git_cli as G  # noqa: E402


class SquashCommitFailureTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t73-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-b", "pi/trunk")
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("base")
        self._git("checkout", "-b", "pi/good")
        self._write("f.txt", "base + feature\n")
        self._write("added_by_feature.txt", "new file from the branch\n")
        self._git("add", "-A")
        self._commit("feat")
        self._git("checkout", "pi/trunk")
        self.head = self._git("rev-parse", "pi/trunk").strip()

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

    def porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def staged(self) -> str:
        return self._git("diff", "--cached", "--name-only").strip()

    def failing_hook(self) -> None:
        hook = self._gitdir() / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\necho "pre-commit hook says no" >&2\nexit 1\n')
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _gitdir(self) -> Path:
        return self.dir / ".git"

    # -- tests ------------------------------------------------------------
    def test_commit_failure_is_cleaned_up(self):
        self.failing_hook()
        with self.assertRaises(RuntimeError) as ctx:
            G.merge_to_trunk(self.dir, "good", "pi/trunk", "title")

        msg = str(ctx.exception)
        self.assertIn("squash commit FAILED", msg)
        self.assertIn("pre-commit hook says no", msg, "git stderr tail missing")
        self.assertIn(self.head[:8], msg, "starting trunk sha missing")

        # the squash staged f.txt and added_by_feature.txt; cleanup undid both
        self.assertEqual(self.porcelain(), "", f"tree left dirty: {self.porcelain()}")
        self.assertEqual(self.staged(), "", "index left staged")
        self.assertFalse((self.dir / "added_by_feature.txt").exists(),
                         "branch-added file survived the cleanup")
        self.assertEqual((self.dir / "f.txt").read_text(), "base\n")
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.head,
                         "HEAD moved on a failed commit")
        self.assertFalse(G.merge_in_progress(self.dir))

    def test_cleanup_failure_raises_and_preserves_evidence(self):
        self.failing_hook()
        with mock.patch.object(G, "abort_merge", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                G.merge_to_trunk(self.dir, "good", "pi/trunk", "title")

        msg = str(ctx.exception)
        self.assertIn("squash commit FAILED", msg)
        self.assertIn("pre-commit hook says no", msg)
        self.assertIn(self.head[:8], msg)
        self.assertIn("cleanup incomplete", msg)
        self.assertIn("added_by_feature.txt", msg, "residual paths not reported")

        # evidence preserved: the staged squash is still there, nothing was reset
        self.assertIn("added_by_feature.txt", self.staged())
        self.assertIn("f.txt", self.staged())
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), self.head)
        self.assertTrue((self.dir / "added_by_feature.txt").exists())
        self.assertTrue((self.dir / "f.txt").read_text().startswith("base + feature"),
                        "worktree was destroyed instead of preserved")

    def test_successful_commit_still_lands(self):
        with mock.patch.object(G, "verify_harness", return_value=(True, "ok")):
            G.merge_to_trunk(self.dir, "good", "pi/trunk", "title")
        log = self._git("log", "--oneline")
        self.assertIn("feat(good): title", log)
        self.assertEqual(self.porcelain(), "")
        self.assertTrue((self.dir / "added_by_feature.txt").exists())


if __name__ == "__main__":
    unittest.main()
