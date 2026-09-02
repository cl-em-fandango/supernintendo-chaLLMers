"""Slice 4 tests: demo deployments are serialized by an exclusive file lock
inside the deploy checkout (FR-8.4). A second concurrent deploy blocks on the
lock and proceeds once it frees.

Run from the repo root:  python3 -m unittest tests.test_demo_deploy_lock
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from external.demo_deploy import (
    LOCKFILE_NAME,
    DemoDeployRequest,
    deploy_lock,
    publish_artifacts,
)

from tests.test_demo_deploy_branch import World, write


class TestDeployLock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.world = World(Path(self._tmp.name))
        write(self.world.artifacts / "index.html", "locked app")

    def test_lock_file_lives_in_the_deploy_checkout(self):
        deploy_dir = self.world.deploy_dir
        with deploy_lock(deploy_dir):
            self.assertTrue((deploy_dir / LOCKFILE_NAME).is_file())

    def test_second_deploy_blocks_on_held_lock_then_proceeds(self):
        lock_held = threading.Event()
        release_lock = threading.Event()

        def hold():
            with deploy_lock(self.world.deploy_dir):
                lock_held.set()
                release_lock.wait(timeout=30)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(lock_held.wait(timeout=5), "holder never took the lock")

        done = threading.Event()
        outcome = []

        def deploy():
            outcome.append(publish_artifacts(self.world.request()))
            done.set()

        deployer = threading.Thread(target=deploy, daemon=True)
        deployer.start()

        # blocked: the deploy must not finish while the lock is held
        self.assertFalse(done.wait(timeout=1.0),
                         "deploy proceeded while the lock was held")
        self.assertNotIn("pi/app-demo", self.world.origin_heads())

        release_lock.set()
        holder.join(timeout=10)
        self.assertTrue(done.wait(timeout=60),
                        "deploy did not proceed after the lock freed")
        self.assertEqual(outcome[0].branch, "pi/app-demo")
        self.assertIn("pi/app-demo", self.world.origin_heads())


if __name__ == "__main__":
    unittest.main()
