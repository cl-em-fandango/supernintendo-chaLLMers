"""T06 — the circuit breaker reverts through `external.git_cli`, never raw git.

`run_loop()`'s breaker used to shell out `git reset --hard pi/last-good` and
`git rev-parse` inline. That bypassed the T05 dirty-tree guard — so a breaker
event could erase uncommitted human work — and duplicated git knowledge outside
the `external/` boundary (CODING_STANDARDS §4). It now calls
`git_cli.revert_to_last_good`, and a refusal (dirty worktree, missing repo, ...)
is a log line the loop survives, never an exception out of the supervisor.
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import supervisor as S  # noqa: E402
from external import git_cli as G  # noqa: E402


def _init_repo(workdir: Path) -> None:
    """A throwaway repo with one commit and a pi/last-good tag on it."""
    def g(*args: str) -> None:
        proc = subprocess.run(["git", *args], cwd=workdir, capture_output=True)
        assert proc.returncode == 0, (args, proc.stderr.decode())

    g("init", "-b", "pi/trunk")
    (workdir / "a").write_text("1")
    g("add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1")
    g("tag", "-f", G.LAST_GOOD_TAG)


class GitCliEntryPointTest(unittest.TestCase):
    """`revert_to_last_good` is the single, guarded entry point for a rollback."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t06-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        _init_repo(self.dir)

    def test_returns_the_ref_it_reverted_to(self):
        (self.dir / "a").write_text("2")
        g = lambda *a: subprocess.run(["git", *a], cwd=self.dir,
                                      capture_output=True, check=True)
        g("add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c2")
        self.assertEqual(G.revert_to_last_good(self.dir, "pi/trunk"),
                         f"tag:{G.LAST_GOOD_TAG}")
        self.assertEqual((self.dir / "a").read_text(), "1")

    def test_refuses_and_preserves_on_a_dirty_tree(self):
        (self.dir / "a").write_text("DIRTY")
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.dir,
                                     capture_output=True, text=True).stdout.strip()
        with self.assertRaises(RuntimeError) as ctx:
            G.revert_to_last_good(self.dir, "pi/trunk")
        self.assertIn("refusing", str(ctx.exception))
        self.assertEqual((self.dir / "a").read_text(), "DIRTY",
                         "dirty edit was clobbered anyway")
        self.assertEqual(
            subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.dir,
                           capture_output=True, text=True).stdout.strip(),
            head_before, "HEAD moved despite the refusal")


class SupervisorNoRawGitTest(unittest.TestCase):
    """F6d: `supervisor.py` must not contain a git command line at all."""

    def test_no_git_literal_in_supervisor_source(self):
        src = Path(S.__file__).read_text()
        self.assertNotIn('"git"', src)
        self.assertIn("revert_to_last_good", src)


class BreakerLoopTest(unittest.TestCase):
    """The breaker calls git_cli, logs the outcome, and always keeps looping."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t06-loop-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = self.dir / "supervisor.log"
        for patch in (
            mock.patch.object(S, "LOG", self.log),
            mock.patch.object(S, "STOPFILE", self.dir / "STOP"),
            mock.patch.object(S, "acquire_lock", lambda: True),
            mock.patch.object(S, "release_lock", lambda: None),
            mock.patch.object(S.signal, "signal", lambda *a, **k: None),
            mock.patch.object(S, "_sleep", lambda stop, seconds: None),
            mock.patch.object(S, "MAX_CYCLES", 1),   # one cycle, then halt
            mock.patch.object(S, "FAIL_LIMIT", 1),   # breaker fires at once
            mock.patch.object(S, "TRUNK", "pi/trunk"),
            mock.patch.object(S, "ChildTracker"),    # harness "fails to launch"
        ):
            patch.start()
            self.addCleanup(patch.stop)
        S.ChildTracker.return_value.spawn.return_value = 1

    def _run_breaker(self, revert: mock.MagicMock) -> tuple[int, str]:
        with mock.patch.object(S, "revert_to_last_good", revert):
            with redirect_stdout(io.StringIO()) as out:
                rc = S.run_loop()
        return rc, out.getvalue()

    def test_revert_goes_through_git_cli_with_the_configured_trunk(self):
        revert = mock.MagicMock(return_value="tag:pi/last-good")
        rc, out = self._run_breaker(revert)
        revert.assert_called_once_with(S.HARNESS_DIR, "pi/trunk")
        self.assertIn("reverted to tag:pi/last-good", out)
        self.assertNotIn("breaker refused", out)
        self.assertEqual(rc, 0)

    def test_refusal_is_logged_and_the_loop_survives(self):
        revert = mock.MagicMock(
            side_effect=RuntimeError("refusing revert pi/trunk to last-good: "
                                     "1 uncommitted paths, e.g. ['config.json']"))
        rc, out = self._run_breaker(revert)
        self.assertIn("CIRCUIT BREAKER", out)
        self.assertIn("⚠ breaker refused: refusing revert pi/trunk", out)
        self.assertNotIn("reverted to", out)
        self.assertEqual(rc, 0, "a refused revert killed the supervisor")
        self.assertIn("supervisor exited", self.log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
