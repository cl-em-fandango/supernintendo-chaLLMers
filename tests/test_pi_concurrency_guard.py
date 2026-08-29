"""Tests for pi child process tracking, hard concurrency limit, and spurious process shutdown."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import external.pi_cli as P


def fake_pi(script_body: str, tmp: Path) -> None:
    """Write an executable `pi` into `tmp` whose body is `script_body`."""
    body = textwrap.indent(textwrap.dedent(script_body).strip("\n"), "    ")
    (tmp / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "try:\n"
        f"{body}\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (tmp / "pi").chmod(0o755)


def _message_end_event(text: str, total_tokens: int) -> str:
    return json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"totalTokens": total_tokens},
            "content": [{"type": "text", "text": text}],
        },
    })


class PiConcurrencyGuardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir()
        self.workdir = Path(self._tmp.name) / "work"
        self.workdir.mkdir()
        self.out_file = self.workdir / "s.out"

        fake_pi("print('stub')", self.bin_dir)

        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))

        orig_limit = P.get_max_concurrent_pi()
        self.addCleanup(lambda: P.set_max_concurrent_pi(orig_limit))

        # Ensure active processes tracker is clean before and after tests
        P.shut_spurious_pi_processes(max_allowed=0)
        self.addCleanup(lambda: P.shut_spurious_pi_processes(max_allowed=0))

    def test_default_limit_is_one(self):
        self.assertEqual(P.get_max_concurrent_pi(), 1)
        P.set_max_concurrent_pi(3)
        self.assertEqual(P.get_max_concurrent_pi(), 3)
        P.set_max_concurrent_pi(1)
        self.assertEqual(P.get_max_concurrent_pi(), 1)

    def test_child_process_registration_and_active_listing(self):
        fake_pi("""
            import time
            time.sleep(2)
            print("done")
        """, self.bin_dir)

        proc = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        try:
            sp = P.register_pi_process(proc, cmd=["pi"], workdir=self.workdir, model="test-m")
            self.assertEqual(sp.pid, proc.pid)
            self.assertEqual(sp.model, "test-m")

            active = P.get_active_pi_processes()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].pid, proc.pid)
            self.assertEqual(P.get_child_pi_pids(), [proc.pid])
        finally:
            proc.terminate()
            proc.wait()
            P.unregister_pi_process(proc.pid)

        self.assertEqual(P.get_active_pi_processes(), [])
        self.assertEqual(P.get_child_pi_pids(), [])

    def test_find_child_pi_pids_detects_child_processes(self):
        fake_pi("""
            import time
            time.sleep(5)
        """, self.bin_dir)

        proc = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        try:
            found = P.find_child_pi_pids()
            self.assertIn(proc.pid, found)
        finally:
            proc.terminate()
            proc.wait()

    def test_identify_and_shut_untracked_spurious_pi(self):
        fake_pi("""
            import time
            time.sleep(10)
        """, self.bin_dir)

        # Spawn an untracked child pi process
        rogue_proc = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        try:
            time.sleep(0.1)
            # Identify spurious processes: rogue_proc is not registered in tracker
            spurious = P.identify_spurious_pi_processes()
            self.assertIn(rogue_proc.pid, spurious)

            # Shut spurious processes
            terminated = P.shut_spurious_pi_processes()
            self.assertIn(rogue_proc.pid, terminated)
            rogue_proc.wait(timeout=2)
            self.assertIsNotNone(rogue_proc.poll())
        finally:
            if rogue_proc.poll() is None:
                rogue_proc.kill()
                rogue_proc.wait()

    def test_identify_and_shut_excess_tracked_pi_instances(self):
        fake_pi("""
            import time
            time.sleep(10)
        """, self.bin_dir)

        P.set_max_concurrent_pi(1)

        p1 = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        p2 = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        try:
            P.register_pi_process(p1, cmd=["pi"], workdir=self.workdir)
            time.sleep(0.05)
            P.register_pi_process(p2, cmd=["pi"], workdir=self.workdir)

            # p2 is excess (spawned after p1, count 2 > max 1)
            spurious = P.identify_spurious_pi_processes(max_allowed=1)
            self.assertIn(p2.pid, spurious)
            self.assertNotIn(p1.pid, spurious)

            terminated = P.shut_spurious_pi_processes(max_allowed=1)
            self.assertIn(p2.pid, terminated)
            p2.wait(timeout=2)
            self.assertIsNotNone(p2.poll())
            self.assertIsNone(p1.poll())
        finally:
            p1.terminate()
            p1.wait()
            p2.poll()
            if p2.returncode is None:
                p2.kill()
                p2.wait()
            P.unregister_pi_process(p1.pid)
            P.unregister_pi_process(p2.pid)

    def test_run_pi_session_cleans_spurious_processes_before_spawn(self):
        fake_pi(f"""
            print({_message_end_event("VERDICT: done", 100)!r})
        """, self.bin_dir)

        # Create a lingering fake pi process
        lingering = subprocess.Popen([str(self.bin_dir / "pi")], cwd=self.workdir)
        time.sleep(0.05)

        # run_pi_session should detect lingering process, shut it down, and run cleanly
        result = P.run_pi_session(
            model="fake-model",
            workdir=self.workdir,
            prompt="test",
            out_file=self.out_file,
            log=lambda *a: None,
        )

        self.assertEqual(result.rc, 0)
        self.assertFalse(result.crashed)
        self.assertEqual(result.output, "VERDICT: done")
        # lingering process should have been terminated
        lingering.wait(timeout=2)
        self.assertIsNotNone(lingering.poll())


if __name__ == "__main__":
    unittest.main()
