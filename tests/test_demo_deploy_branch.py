"""Slice 4 tests: the deployer core publishes artifacts to `docs/` on
`pi/app-demo` in a dedicated checkout (FR-5), against temp git repos and a
`git init --bare` fake origin. No LLM, no npm, no network.

Run from the repo root:  python3 -m unittest tests.test_demo_deploy_branch
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from external.demo_deploy import (
    DemoDeployError,
    DemoDeployRequest,
    DeployStep,
    origin_url_from_repo,
    publish_artifacts,
)

DEPLOY_BRANCH = "pi/app-demo"
TRUNK = "pi/trunk"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=test@example.com",
         "-c", "user.name=Test", *args],
        cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class World:
    """temp harness repo + bare fake origin + deploy checkout + artifacts."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        git(tmp, "init", "--bare", "-b", TRUNK, str(self.origin))
        self.harness = tmp / "harness"
        self.harness.mkdir()
        git(self.harness, "init", "-b", TRUNK)
        write(self.harness / "app.txt", "v1\n")
        write(self.harness / "docs_src" / "note.md", "trunk stub\n")
        git(self.harness, "add", "-A")
        git(self.harness, "commit", "-m", "trunk v1")
        git(self.harness, "remote", "add", "origin", str(self.origin))
        git(self.harness, "push", "origin", TRUNK)
        self.artifacts = tmp / "artifacts"
        self.artifacts.mkdir()
        self.deploy_dir = tmp / "deploy"

    def request(self) -> DemoDeployRequest:
        return DemoDeployRequest(
            harness_repo=self.harness,
            deploy_dir=self.deploy_dir,
            origin_url=str(self.origin),
            deploy_branch=DEPLOY_BRANCH,
            trunk_branch=TRUNK,
            docs_dir="docs",
            artifacts_dir=self.artifacts,
        )

    def trunk_commit(self, path: str, text: str) -> str:
        """Commit on the harness repo's local trunk; never pushed."""
        write(self.harness / path, text)
        git(self.harness, "add", "-A")
        git(self.harness, "commit", "-m", f"trunk: {path}")
        return git(self.harness, "rev-parse", TRUNK).strip()

    def human_commit_on_deploy(self, path: str, text: str) -> None:
        """A commit pushed straight to origin's deploy branch."""
        scratch = self.tmp / f"scratch-{len(list(self.tmp.glob('scratch-*')))}"
        git(self.tmp, "clone", str(self.origin), str(scratch))
        git(scratch, "checkout", "-B", DEPLOY_BRANCH,
            f"origin/{DEPLOY_BRANCH}")
        write(scratch / path, text)
        git(scratch, "add", "-A")
        git(scratch, "commit", "-m", f"human: {path}")
        git(scratch, "push", "origin", f"HEAD:{DEPLOY_BRANCH}")

    def origin_heads(self) -> dict[str, str]:
        out = git(self.origin, "ls-remote", "--heads", ".")
        heads = {}
        for line in out.splitlines():
            sha, ref = line.split()
            heads[ref.removeprefix("refs/heads/")] = sha
        return heads

    def origin_tree(self, branch: str) -> list[str]:
        return sorted(git(self.origin, "ls-tree", "-r", "--name-only",
                          branch).split())

    def origin_blob(self, branch: str, path: str) -> str:
        return git(self.origin, "cat-file", "blob", f"{branch}:{path}")

    def origin_has_ancestor(self, sha: str, branch: str) -> bool:
        return subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, branch],
            cwd=str(self.origin)).returncode == 0


class TestPublishToDeployBranch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))

    def test_first_publish_creates_branch_with_exact_docs(self):
        write(self.world.artifacts / "index.html", "<html>app</html>")
        write(self.world.artifacts / "assets" / "app.js", "console.log(1)")
        write(self.world.artifacts / ".nojekyll", "")

        outcome = publish_artifacts(self.world.request())

        self.assertEqual(outcome.branch, DEPLOY_BRANCH)
        self.assertTrue(outcome.changed)
        self.assertEqual(self.world.origin_tree(DEPLOY_BRANCH),
                         sorted(["docs/.nojekyll", "docs/assets/app.js",
                                 "docs/index.html", "app.txt",
                                 "docs_src/note.md"]))
        self.assertEqual(self.world.origin_blob(DEPLOY_BRANCH,
                                                "docs/index.html"),
                         "<html>app</html>")

    def test_trunk_refreshed_from_local_harness_and_rebased(self):
        publish_artifacts(self.world.request())  # creates branch from trunk v1
        stale_trunk = self.world.origin_heads()[TRUNK]
        # a trunk-only commit in the harness workdir, never pushed to origin
        new_trunk = self.world.trunk_commit("app.txt", "v2\n")

        publish_artifacts(self.world.request())

        # (b) the trunk-only commit is an ancestor of the deploy tip
        self.assertTrue(self.world.origin_has_ancestor(new_trunk,
                                                       DEPLOY_BRANCH))
        # (d) origin trunk untouched by the deployer (still the old sha)
        self.assertEqual(self.world.origin_heads()[TRUNK], stale_trunk)

    def test_branch_created_when_absent_on_origin(self):
        self.assertNotIn(DEPLOY_BRANCH, self.world.origin_heads())
        write(self.world.artifacts / "index.html", "x")
        publish_artifacts(self.world.request())
        self.assertIn(DEPLOY_BRANCH, self.world.origin_heads())

    def test_stale_docs_fully_replaced(self):
        write(self.world.artifacts / "a.txt", "old")
        write(self.world.artifacts / "sub" / "b.txt", "old")
        publish_artifacts(self.world.request())

        fresh = self.world.tmp / "artifacts2"
        write(fresh / "d.txt", "new")
        publish_artifacts(dataclasses.replace(self.world.request(),
                                              artifacts_dir=fresh))

        docs = [p for p in self.world.origin_tree(DEPLOY_BRANCH)
                if p.startswith("docs/")]
        self.assertEqual(docs, ["docs/d.txt"])
        self.assertEqual(self.world.origin_blob(DEPLOY_BRANCH, "docs/d.txt"),
                         "new")

    def test_only_deploy_branch_is_pushed(self):
        before = set(self.world.origin_heads())
        publish_artifacts(self.world.request())
        after = self.world.origin_heads()
        self.assertEqual(set(after) - before, {DEPLOY_BRANCH})
        self.assertNotIn(".deploy.lock", self.world.origin_tree(DEPLOY_BRANCH))

    def test_idempotent_redeploy_of_identical_artifacts(self):
        write(self.world.artifacts / "index.html", "same")
        first = publish_artifacts(self.world.request())
        second = publish_artifacts(self.world.request())
        self.assertEqual(first.commit, second.commit)
        self.assertFalse(second.changed)


class TestRepeatDeployBuildResidue(unittest.TestCase):
    """The in-checkout builder (FR-7.3) leaves npm residue in the tracked
    tree; a repeat deploy in the same checkout must still succeed
    (FR-8.4, spec edge case 3). Both reproduced failure modes are
    covered: a tracked file the build rewrites, and an untracked file
    the build creates that later merges to trunk."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))
        self.rounds = 0

    def _npm_residue_builder(self, lock_path: str):
        """A builder mimicking `npm install`: rewrites/creates the lock
        file and drops `node_modules/` into the tracked checkout."""
        def builder(checkout: Path) -> Path:
            lock = checkout / lock_path
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text("rewritten by npm\n")
            node_modules = lock.parent / "node_modules"
            node_modules.mkdir(exist_ok=True)
            (node_modules / "dep.js").write_text("x\n")
            self.rounds += 1
            artifacts = self.world.tmp / f"artifacts-{self.rounds}"
            write(artifacts / "index.html", f"build {self.rounds}\n")
            return artifacts
        return builder

    def _request(self, builder) -> DemoDeployRequest:
        return dataclasses.replace(self.world.request(),
                                   artifacts_dir=None, builder=builder)

    def test_tracked_lock_rewritten_by_build_does_not_block_redeploy(self):
        self.world.trunk_commit("demo-apps/active/package-lock.json",
                                "lock v1\n")
        request = self._request(
            self._npm_residue_builder("demo-apps/active/package-lock.json"))

        publish_artifacts(request)
        outcome = publish_artifacts(request)  # second deploy, dirty tree

        self.assertEqual(self.world.origin_blob(
            DEPLOY_BRANCH, "docs/index.html"), "build 2\n")
        self.assertTrue(outcome.changed)

    def test_untracked_residue_later_merged_to_trunk_does_not_block(self):
        request = self._request(
            self._npm_residue_builder("demo-apps/active/package-lock.json"))
        publish_artifacts(request)  # npm creates the lock untracked
        self.world.trunk_commit("demo-apps/active/package-lock.json",
                                "lock merged to trunk\n")

        publish_artifacts(request)  # checkout would clobber the residue

        self.assertEqual(self.world.origin_blob(
            DEPLOY_BRANCH, "docs/index.html"), "build 2\n")
        # build residue is never pushed (the clean runs before each
        # deploy; the builder's own residue from this run stays local)
        self.assertNotIn("demo-apps/active/node_modules/dep.js",
                         self.world.origin_tree(DEPLOY_BRANCH))
        # the lock file on the deploy branch is trunk's, not npm's
        self.assertEqual(self.world.origin_blob(
            DEPLOY_BRANCH, "demo-apps/active/package-lock.json"),
            "lock merged to trunk\n")


class TestOriginUrlFromRepo(unittest.TestCase):
    """The origin-URL helper the composition path derives the deploy
    checkout's origin from (slice 5's placeholder hook uses it)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))

    def test_reads_the_origin_url_of_a_clone(self):
        self.assertEqual(origin_url_from_repo(self.world.harness),
                         str(self.world.origin))

    def test_repo_without_origin_raises(self):
        bare = self.world.tmp / "no-origin"
        bare.mkdir()
        git(bare, "init")
        with self.assertRaises(RuntimeError):
            origin_url_from_repo(bare)


class TestRebaseConflicts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))
        publish_artifacts(self.world.request())  # branch exists on origin

    def test_conflict_outside_docs_raises_hard_failure(self):
        self.world.human_commit_on_deploy("README.md", "human line\n")
        self.world.trunk_commit("README.md", "trunk line\n")
        before = self.world.origin_heads()[DEPLOY_BRANCH]

        with self.assertRaises(DemoDeployError) as ctx:
            publish_artifacts(self.world.request())

        self.assertEqual(ctx.exception.step, DeployStep.REBASE)
        self.assertIn("README.md", str(ctx.exception))
        # previous deployment intact
        self.assertEqual(self.world.origin_heads()[DEPLOY_BRANCH], before)

    def test_docs_only_conflict_resolves_to_regenerated_docs(self):
        write(self.world.artifacts / "index.html", "first")
        publish_artifacts(self.world.request())
        self.world.human_commit_on_deploy("docs/index.html", "human")
        self.world.trunk_commit("docs/index.html", "trunk")

        fresh = self.world.tmp / "artifacts-final"
        write(fresh / "index.html", "final")
        publish_artifacts(dataclasses.replace(self.world.request(),
                                              artifacts_dir=fresh))

        docs = [p for p in self.world.origin_tree(DEPLOY_BRANCH)
                if p.startswith("docs/")]
        self.assertEqual(docs, ["docs/index.html"])
        self.assertEqual(self.world.origin_blob(DEPLOY_BRANCH,
                                                "docs/index.html"), "final")


if __name__ == "__main__":
    unittest.main()
