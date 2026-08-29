"""T65 — the gate-not-applicable refusal happens before git is touched.

`gate_applies` (T24) is the two-file recognition check that stands in front of
`merge_to_trunk`: a repo without `harness.py` *and* `harness/composition.py` is
a repo `verify_harness` cannot honestly judge, so the merge is refused outright
instead of being merged and reverted behind an undeclared gate. That refusal is
only harmless if it really comes *first*: by the time `GateNotApplicable`
propagates, HEAD, the index and the worktree must be exactly what they were and
no git command must have run at all.

Covered:
  * a temp non-harness repo raises `GateNotApplicable`, with a message naming
    the task, the repo and the reason,
  * HEAD, the current branch, every ref, the index, `git status`, the reflog and
    every worktree file are unchanged by the refusal,
  * no git subprocess call of any kind is made before the refusal — recorded by
    patching the module's own `subprocess`, the single door every git call in
    `external/git_cli.py` goes through,
  * the refusal outranks the other guards (a dirty tree and a missing feature
    branch still produce `GateNotApplicable`, not a dirty-guard error or a git
    failure) and never consults `verify_harness`,
  * `gate_applies` itself: both stub files or neither, and no subprocess,
  * a control proving the recorder is not blind: the same repo *with* the
    recognition stubs does reach git write commands, so "no write recorded"
    above is a fact about the refusal and not about the instrumentation.

Out of scope (T63, T64, T72 and T73 own them): a successful gated merge, merge
conflicts, the dirty-tree revert refusal, per-repo gate design (decision D3),
and anything run against this repo's real `.git`.

Run from the repo root:  python3 -m unittest tests.test_gate_not_applicable -v
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
TASK = "t1"
BRANCH = f"pi/{TASK}"

# git subcommands that change the repository. Everything else this module calls
# (`status`, `rev-parse`, `diff`, `ls-files`, `show-ref`) is a read.
GIT_WRITE_COMMANDS = frozenset({
    "add", "am", "apply", "branch", "checkout", "clean", "commit", "gc",
    "maintenance", "merge", "mv", "push", "rebase", "reset", "restore", "rm",
    "stash", "switch", "tag",
})

# Files a merge, a revert or an interrupted write would leave in `.git`. None of
# them exists in a repo that was only initialized, committed and branched.
GIT_RESIDUE = ("MERGE_HEAD", "ORIG_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
               "AUTO_MERGE", "MERGE_MSG", "index.lock")


class GitWriteAttempted(RuntimeError):
    """Raised by the recorder *instead of* executing a write command, so a test
    can prove the recorder sees writes without letting a merge happen."""


def git_subcommand(argv: list[str]) -> str:
    """The subcommand of a git argv, skipping global `-c <key=value>` flags.

    `merge_to_trunk` builds author identities as `-c user.email=... -c
    user.name=...` ahead of `commit`, so the subcommand is not simply the second
    token — classifying by position alone would call that invocation a read.
    """
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "-c":
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return ""


class RecordingGit:
    """A stand-in for the `subprocess` module that records every invocation.

    Delegates to the real `subprocess.run`, so git effects stay real; with
    `block_writes=True` a write command raises `GitWriteAttempted` before it
    runs. Patched onto `external.git_cli.subprocess`, the only way that module
    reaches git (`_git`, `has_branch`, `has_tag`, `dirty_paths`, the squash, the
    commit, `verify_harness`), so an empty `calls` list genuinely means "no git
    command ran at all".
    """

    def __init__(self, block_writes: bool = False):
        self.block_writes = block_writes
        self.calls: list[list[str]] = []

    def run(self, *args, **kwargs):
        argv = [str(a) for a in args[0]] if args and isinstance(
            args[0], (list, tuple)) else []
        self.calls.append(argv)
        if self.block_writes and git_subcommand(argv) in GIT_WRITE_COMMANDS:
            raise GitWriteAttempted(" ".join(argv))
        return subprocess.run(*args, **kwargs)

    @property
    def writes(self) -> list[str]:
        """The subcommands of every recorded invocation that would write."""
        return [git_subcommand(argv) for argv in self.calls
                if git_subcommand(argv) in GIT_WRITE_COMMANDS]


class GateRefusalFixture(unittest.TestCase):
    """A temp repo the harness gate cannot judge: plain files, trunk, and a
    feature branch that would merge cleanly, so the refusal cannot be blamed on a
    missing branch or a conflict.

    Every repo lives under its own `tempfile.mkdtemp()` root; nothing here
    touches this working tree or `/home/donald/work/queue`.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="t65-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._git("init", "-b", TRUNK)
        # Keep git from spawning background object writes: the `.git` residue
        # check must only ever see files a merge would have created.
        self._git("config", "gc.auto", "0")
        self._git("config", "maintenance.auto", "0")
        self._write("f.txt", "base\n")
        self._git("add", "-A")
        self._commit("base")
        self._git("checkout", "-b", BRANCH)
        self._write("f.txt", "base + feature\n")
        self._write("feature.txt", "work product from the branch\n")
        self._git("add", "-A")
        self._commit("feat")
        self._git("checkout", TRUNK)
        self.assertFalse(G.gate_applies(self.dir),
                         "the fixture is not a non-harness repo")

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

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def refs(self) -> str:
        """Every branch and tag together with the commit it resolves to."""
        return self._git("show-ref").strip()

    def porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()

    def index(self) -> str:
        """The index itself: mode, blob, stage and path for every entry."""
        return self._git("ls-files", "--stage").strip()

    def unmerged(self) -> str:
        return self._git("ls-files", "-u").strip()

    def reflog(self) -> str:
        return self._git("reflog", "show", "HEAD").strip()

    def residue(self) -> list[str]:
        gitdir = G._gitdir(self.dir)
        self.assertIsNotNone(gitdir, "fixture lost its .git directory")
        return [name for name in GIT_RESIDUE if (gitdir / name).exists()]

    def worktree_texts(self) -> dict[str, str]:
        """Every file in the worktree, keyed by rel path, contents as text."""
        texts: dict[str, str] = {}
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.dir)
            if ".git" in rel.parts:
                continue
            texts[str(rel)] = p.read_text()
        return texts

    def snapshot(self) -> dict[str, object]:
        """Everything a git write could change, captured in one comparison."""
        return {
            "head": self.head(),
            "branch": self.current_branch(),
            "refs": self.refs(),
            "status": self.porcelain(),
            "index": self.index(),
            "unmerged": self.unmerged(),
            "reflog": self.reflog(),
            "residue": self.residue(),
            "files": self.worktree_texts(),
        }

    def add_gate_stubs(self) -> None:
        """The two files `gate_applies` recognizes as the harness repo. Stubs
        only: the tree is never importable and the real gate is never run.

        They are committed, not merely written — left untracked they would trip
        the `_require_clean` guard that sits behind the gate check, and a control
        that stops at the dirty guard proves nothing about the checkout."""
        self._write("harness.py", "# gate recognition stub\n")
        self._write("harness/composition.py", "# gate recognition stub\n")
        self._git("add", "-A")
        self._commit("gate recognition stubs")
        self.assertEqual(self.porcelain(), "", "stub commit left the tree dirty")

    def refused(self, block_writes: bool = False):
        """Run the merge on a repo the gate cannot judge.

        Returns (message, recorder). With `block_writes` the recorder refuses to
        execute a write command rather than running it, so a test asking for it
        cannot be satisfied by a merge that quietly went ahead.
        """
        rec = RecordingGit(block_writes=block_writes)
        with mock.patch.object(G, "subprocess", new=rec):
            with self.assertRaises(G.GateNotApplicable) as ctx:
                G.merge_to_trunk(self.dir, TASK, TRUNK, "title")
        return str(ctx.exception), rec


class GateNotApplicableRefusalTest(GateRefusalFixture):
    def test_refusal_raises_gate_not_applicable(self):
        with self.assertRaises(G.GateNotApplicable):
            G.merge_to_trunk(self.dir, TASK, TRUNK, "title")

    def test_gate_not_applicable_is_a_runtime_error(self):
        """The pipeline's generic `except RuntimeError` handling must still see
        it, whatever the dedicated `stage_holistic` branch does with it."""
        self.assertTrue(issubclass(G.GateNotApplicable, RuntimeError))

    def test_message_names_the_task_the_repo_and_the_reason(self):
        msg, _ = self.refused()
        self.assertIn("no verification gate is defined for this repo", msg)
        self.assertIn(TASK, msg, "the refusal does not name the task")
        self.assertIn(str(self.dir), msg, "the refusal does not name the repo")

    def test_refusal_leaves_head_branch_and_history_untouched(self):
        before = self.snapshot()

        self.refused()

        after = self.snapshot()
        self.assertEqual(after["head"], before["head"], "HEAD moved")
        self.assertEqual(after["branch"], before["branch"], "branch switched")
        self.assertEqual(after["refs"], before["refs"], "a ref was created or moved")
        self.assertEqual(after["reflog"], before["reflog"],
                         "the reflog grew: HEAD was moved by something")

    def test_refusal_leaves_the_index_untouched(self):
        before = self.snapshot()

        self.refused()

        after = self.snapshot()
        self.assertEqual(after["index"], before["index"], "index entries changed")
        self.assertEqual(after["unmerged"], "", "unmerged index entries appeared")

    def test_refusal_leaves_the_status_untouched(self):
        before = self.snapshot()
        self.assertEqual(before["status"], "", "fixture started dirty")

        self.refused()

        after = self.snapshot()
        self.assertEqual(after["status"], before["status"],
                         f"tree left dirty: {after['status']}")
        self.assertEqual(after["files"], before["files"],
                         "worktree files changed by the refusal")

    def test_refusal_makes_no_git_call_at_all(self):
        """`gate_applies` is a pure predicate, so the refusal is not merely
        write-free: not one git process is spawned before it raises."""
        _, rec = self.refused(block_writes=True)
        self.assertEqual(rec.calls, [],
                         f"git ran before the refusal: {rec.calls}")

    def test_refusal_reaches_no_write_command(self):
        """The card's own wording, asserted directly: with writes armed to blow
        up, the refusal still wins, so nothing was written."""
        msg, rec = self.refused(block_writes=True)
        self.assertIn("no verification gate", msg)
        self.assertEqual(rec.writes, [], "a git write command was reached")

    def test_refusal_creates_no_merge_residue_and_no_last_good_tag(self):
        self.refused()
        self.assertEqual(self.residue(), [],
                         f".git left with merge residue: {self.residue()}")
        self.assertFalse(G.merge_in_progress(self.dir))
        self.assertFalse(G.has_tag(self.dir, G.LAST_GOOD_TAG),
                         "the refusal advanced pi/last-good")

    def test_refusal_keeps_the_feature_branch_for_a_human(self):
        branch_sha = self._git("rev-parse", BRANCH).strip()

        self.refused()

        self.assertTrue(G.has_branch(self.dir, BRANCH),
                        "the refusal deleted the feature branch")
        self.assertEqual(self._git("rev-parse", BRANCH).strip(), branch_sha,
                         "the refusal moved the feature branch")

    def test_refusal_never_consults_the_verification_gate(self):
        """The refusal replaces the merge, it does not gate it: a gate that ran
        here would be judging an unmerged trunk."""
        gate = mock.Mock(return_value=(True, "ok"))
        with mock.patch.object(G, "verify_harness", gate):
            with self.assertRaises(G.GateNotApplicable):
                G.merge_to_trunk(self.dir, TASK, TRUNK, "title")
        gate.assert_not_called()

    def test_refusal_is_repeatable_and_state_stays_identical(self):
        before = self.snapshot()

        self.refused()
        self.refused()

        self.assertEqual(self.snapshot(), before, "the second refusal changed state")

    def test_plain_directory_is_refused_without_git_init(self):
        """A workdir that is not a repo at all must not be initialized on the
        way to the refusal."""
        plain = self.dir.parent / f"{self.dir.name}-plain"
        plain.mkdir()
        self.addCleanup(shutil.rmtree, plain, ignore_errors=True)

        rec = RecordingGit(block_writes=True)
        with mock.patch.object(G, "subprocess", new=rec):
            with self.assertRaises(G.GateNotApplicable):
                G.merge_to_trunk(plain, TASK, TRUNK, "title")

        self.assertFalse((plain / ".git").exists(), "the refusal created a repo")
        self.assertEqual(rec.calls, [], f"git ran before the refusal: {rec.calls}")


class RefusalOutranksOtherGuardsTest(GateRefusalFixture):
    """The gate check is the *first* statement of `merge_to_trunk`, so it wins
    over every later guard. Each test here makes the repo fail a later guard too
    and asserts the exception is still `GateNotApplicable`."""

    def test_dirty_tree_still_refuses_on_the_gate(self):
        """A dirty non-harness repo must not get the T05 dirty-guard message:
        the repo was never going to be merged, dirty or not."""
        self._write("f.txt", "uncommitted local work\n")
        self._write("staged_work.txt", "staged, never committed\n")
        self._git("add", "staged_work.txt")
        self._write("written_by_another_tool.txt", "untracked\n")
        before = self.snapshot()
        self.assertTrue(before["status"], "dirty state not established")

        msg, rec = self.refused(block_writes=True)

        self.assertIn("no verification gate", msg)
        self.assertNotIn("uncommitted", msg,
                         "the dirty guard answered instead of the gate check")
        self.assertEqual(self.snapshot(), before,
                         "the refusal altered the dirty state")
        self.assertEqual(rec.calls, [], f"git ran before the refusal: {rec.calls}")

    def test_missing_feature_branch_still_refuses_on_the_gate(self):
        """No branch to merge is a git failure, not the reason for this refusal;
        the gate check has to come first for the message to be the honest one."""
        self._git("branch", "-D", BRANCH)
        self.assertFalse(G.has_branch(self.dir, BRANCH))

        msg, rec = self.refused(block_writes=True)

        self.assertIn("no verification gate", msg)
        self.assertEqual(self.current_branch(), TRUNK)
        self.assertEqual(rec.calls, [], f"git ran before the refusal: {rec.calls}")

    def test_refusal_precedes_the_trunk_checkout(self):
        """Order proof: instrument the module's own git entry point so the first
        command raises. On a non-harness repo nothing is reached at all, so the
        sentinel never fires and `GateNotApplicable` propagates."""
        calls: list[tuple] = []

        def spy(cwd, *args, **kwargs):
            calls.append(args)
            raise GitWriteAttempted(" ".join(args))

        with mock.patch.object(G, "_git", side_effect=spy):
            with self.assertRaises(G.GateNotApplicable):
                G.merge_to_trunk(self.dir, TASK, TRUNK, "title")

        self.assertEqual(calls, [], f"git commands before the refusal: {calls}")
        self.assertEqual(self.current_branch(), TRUNK, "trunk was checked out")


class GateAppliesRecognitionTest(GateRefusalFixture):
    """`gate_applies` is the cause of the refusal, so the refusal's precision
    depends on it: both recognition files or neither, and no subprocess."""

    def test_plain_repo_does_not_apply(self):
        self.assertFalse(G.gate_applies(self.dir))

    def test_harness_py_alone_does_not_apply(self):
        self._write("harness.py", "# stub\n")
        self.assertFalse(G.gate_applies(self.dir),
                         "harness.py alone was treated as the harness repo")

    def test_composition_py_alone_does_not_apply(self):
        self._write("harness/composition.py", "# stub\n")
        self.assertFalse(G.gate_applies(self.dir),
                         "harness/composition.py alone was treated as the harness")

    def test_both_recognition_files_apply(self):
        """Control for the refusal tests: adding the two stubs is the only thing
        that separates this repo from the ones that raise."""
        self.add_gate_stubs()
        self.assertTrue(G.gate_applies(self.dir))

    def test_directories_are_not_recognized_as_files(self):
        (self.dir / "harness.py").mkdir()
        (self.dir / "harness" / "composition.py").mkdir(parents=True)
        self.assertFalse(G.gate_applies(self.dir),
                         "directories were accepted where files are required")

    def test_missing_path_does_not_apply(self):
        missing = self.dir / "does-not-exist"
        self.assertFalse(G.gate_applies(missing))

    def test_string_arguments_are_accepted(self):
        """A `TypeError` from inside a guard is how a guard gets removed, so the
        predicate takes `str` as happily as `Path`."""
        self.add_gate_stubs()
        self.assertTrue(G.gate_applies(str(self.dir)))
        self.assertFalse(G.gate_applies(str(self.dir / "nope")))

    def test_predicate_shells_out_nothing(self):
        """The refusal can only be git-free if the check behind it is: two file
        probes, no subprocess, for both accepted argument types."""
        rec = RecordingGit()
        with mock.patch.object(G, "subprocess", new=rec):
            self.assertFalse(G.gate_applies(self.dir))
            self.assertFalse(G.gate_applies(str(self.dir)))
        self.assertEqual(rec.calls, [],
                         f"the recognition predicate ran git: {rec.calls}")


class RecorderSeesWritesTest(GateRefusalFixture):
    """Control for every "no git write" assertion above: the same recorder and
    the same repo, once it carries the recognition stubs, does reach git write
    commands. Without this, an empty call list could mean blind instrumentation.
    """

    def attempted(self):
        """Merge a repo the gate *can* judge, with writes blocked so the merge
        itself (T63's subject) never runs. Returns (sentinel, recorder)."""
        self.add_gate_stubs()
        self.assertTrue(G.gate_applies(self.dir))
        rec = RecordingGit(block_writes=True)
        with mock.patch.object(G, "subprocess", new=rec):
            with self.assertRaises(GitWriteAttempted) as ctx:
                G.merge_to_trunk(self.dir, TASK, TRUNK, "title")
        return str(ctx.exception), rec

    def test_a_judgable_repo_is_not_refused_by_the_gate(self):
        sentinel, _ = self.attempted()
        self.assertNotIn("no verification gate", sentinel)
        self.assertIn("checkout", sentinel,
                      "the run stopped somewhere other than the trunk checkout")

    def test_a_judgable_repo_reaches_git_write_commands(self):
        _, rec = self.attempted()
        self.assertTrue(rec.calls, "no git command was recorded at all")
        self.assertIn("checkout", rec.writes,
                      f"no write command reached, only reads: {rec.calls}")

    def test_the_first_write_reached_is_the_trunk_checkout(self):
        """The gate check sits in front of the checkout, so on a repo it accepts
        the checkout is the first write attempted — and on a repo it refuses,
        even that never happens."""
        _, rec = self.attempted()
        self.assertEqual(rec.writes[0], "checkout",
                         f"first write was {rec.writes[0]}, expected checkout")


if __name__ == "__main__":
    unittest.main()
