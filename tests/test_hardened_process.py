"""Slice 4.2/4.3/4.4: the hardened subprocess runner (`external/hardened_process.py`).

Real `Popen` processes, no model and no network. The cases pin the three
guardrails of FR-4:

- timeout: a command that outlives `timeout_s` is stopped as a *process
  group* — the sleeping grandchild dies with it, not orphaned — and the
  result is flagged `timed_out`, never silently reported as success;
- output caps: a spewing child is read to EOF (so it never blocks on the pipe
  buffer) but only `max_output_bytes` are kept, and the matching
  `stdout_truncated`/`stderr_truncated` flag is set;
- ulimits: `run_bash` wraps the command so `ulimit -u`/`ulimit -v` inside the
  shell report the configured values.

Run from the repo root:  python3 -m unittest tests.test_hardened_process
"""
from __future__ import annotations

import os
import signal
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from external.hardened_process import (
    CommandResult,
    GuardrailLimits,
    run,
    run_bash,
)

# Wall guard for the timeout cases: timeout + grace + the drainer joins.
CASE_GUARD_S = 20


def _pid_alive(pid: int) -> bool:
    """True while `pid` exists as a *live* process (a reaped zombie is dead)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # A signalled-but-not-reaped child of *this* process would answer
        # alive to signal 0; distinguish a zombie via /proc when available.
        status = Path(f"/proc/{pid}/stat").read_text()
        return status[status.rfind(")") + 1:].split()[0] != "Z"
    except (FileNotFoundError, IndexError, PermissionError):
        return True


def _wait_for_death(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TimeoutGroupKillTest(unittest.TestCase):
    """FR-4.1: the timeout takes the whole tree down, and says so."""

    def test_sleeping_grandchild_dies_with_the_group(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            # `sleep 60 &` runs in the same process group as the bash parent
            # (non-interactive bash has no job control), so a group kill gets
            # both; a bare proc.terminate() would orphan the sleep.
            result = run(
                ["bash", "-c", f"sleep 60 & echo $! > '{pid_file}'; wait"],
                limits=GuardrailLimits(timeout_s=1.0, terminate_grace_s=1.0),
            )
            self.assertTrue(result.timed_out, "run must report the timeout")
            self.assertNotEqual(result.rc, 0, "a timed-out run is not a success")

            grandchild = int(pid_file.read_text().strip())
            self.assertTrue(
                _wait_for_death(grandchild),
                f"orphaned grandchild pid {grandchild} survived the group kill",
            )

    def test_fast_command_is_not_flagged_timed_out(self):
        result = run(["bash", "-c", "echo hi"],
                     limits=GuardrailLimits(timeout_s=10.0))
        self.assertFalse(result.timed_out)
        self.assertEqual(result.rc, 0)
        self.assertEqual(result.stdout.strip(), "hi")

    def test_child_exit_code_survives(self):
        result = run(["bash", "-c", "exit 3"],
                     limits=GuardrailLimits(timeout_s=10.0))
        self.assertFalse(result.timed_out)
        self.assertEqual(result.rc, 3)

    def test_sigterm_gets_the_grace_to_run_a_trap(self):
        """A child that exits on SIGTERM must not need the SIGKILL."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "caught"
            result = run(
                ["bash", "-c",
                 f"trap 'touch {marker}; exit 7' TERM; sleep 30 & wait"],
                limits=GuardrailLimits(timeout_s=1.0, terminate_grace_s=5.0),
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(marker.exists(),
                            "SIGTERM-first grace was skipped (SIGKILL-first)")


class OutputCapTest(unittest.TestCase):
    """FR-4.2: capture is capped per stream, overflow discarded and flagged."""

    def test_stdout_beyond_cap_is_discarded_and_flagged(self):
        spew = "import sys; sys.stdout.write('a' * 300_000)"
        result = run(["python3", "-c", spew],
                     limits=GuardrailLimits(timeout_s=30.0,
                                            max_output_bytes=1024))
        self.assertEqual(result.rc, 0,
                         "the child must not notice the cap (read to EOF)")
        self.assertEqual(len(result.stdout), 1024)
        self.assertTrue(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)

    def test_default_cap_is_two_mebibytes(self):
        spew = "import sys; sys.stdout.write('b' * 3_000_000)"
        result = run(["python3", "-c", spew], limits=GuardrailLimits(timeout_s=30.0))
        self.assertEqual(len(result.stdout), 2 * 1024 * 1024)
        self.assertTrue(result.stdout_truncated)

    def test_stderr_is_capped_independently_of_stdout(self):
        spew = ("import sys\n"
                "sys.stdout.write('o' * 5000)\n"
                "sys.stderr.write('e' * 5000)\n")
        result = run(["python3", "-c", spew],
                     limits=GuardrailLimits(timeout_s=30.0,
                                            max_output_bytes=1000))
        self.assertEqual(len(result.stdout), 1000)
        self.assertEqual(len(result.stderr), 1000)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)

    def test_under_cap_keeps_everything_unflagged(self):
        result = run(["bash", "-c", "echo short"],
                     limits=GuardrailLimits(timeout_s=10.0,
                                            max_output_bytes=1024))
        self.assertEqual(result.stdout, "short\n")
        self.assertFalse(result.stdout_truncated)
        self.assertFalse(result.stderr_truncated)


class UlimitWrapperTest(unittest.TestCase):
    """FR-4.3: `run_bash` executes under the configured nproc/vmem ulimits."""

    def test_default_ulimits_are_applied(self):
        nproc = run_bash("ulimit -u", limits=GuardrailLimits(timeout_s=10.0))
        self.assertEqual(nproc.rc, 0)
        self.assertEqual(nproc.stdout.strip(), "50")

        vmem = run_bash("ulimit -v", limits=GuardrailLimits(timeout_s=10.0))
        self.assertEqual(vmem.rc, 0)
        self.assertEqual(vmem.stdout.strip(), "8388608")

    def test_configured_ulimits_are_applied(self):
        limits = GuardrailLimits(timeout_s=10.0,
                                 ulimit_nproc=64,
                                 ulimit_vmem_kb=1048576)
        nproc = run_bash("ulimit -u", limits=limits)
        self.assertEqual(nproc.stdout.strip(), "64")
        vmem = run_bash("ulimit -v", limits=limits)
        self.assertEqual(vmem.stdout.strip(), "1048576")

    def test_wrapped_command_output_and_rc_pass_through(self):
        result = run_bash("echo wrapped; exit 5",
                          limits=GuardrailLimits(timeout_s=10.0))
        self.assertEqual(result.stdout.strip(), "wrapped")
        self.assertEqual(result.rc, 5)


if __name__ == "__main__":
    unittest.main()
