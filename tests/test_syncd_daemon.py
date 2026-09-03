"""Slice 11 — run lock + the `harness syncd` daemon (spec FR-4).

Covered here, all in-process (NFR-5: temp dirs, injected fakes, no real
subprocess, no HTTP):

  * `ProcessLock`: acquire/release, a live PID holds (AC-10 mechanism),
    a dead or corrupt PID is stale and recovered (AC-11);
  * `SyncdLoop`: one sync pass and at most one spawn per pass (AC-9),
    no spawn on an empty `pending/`, a held `run.lock`, or a live child;
    `sync=None` (GitHub unconfigured) skips the pass and stays a local
    watcher (FR-0.1); a second daemon exits non-zero with the lock
    message (AC-10); a stale `syncd.lock` is recovered (AC-11);
  * failure backoff (FR-4.5): 5x interval at N=5, exactly one warning
    per backoff entry, reset on a successful pass;
  * signals (FR-4.6): stop ends the loop after the current pass, the
    lock is removed, exit is 0 — including the SIGTERM handler wiring;
  * the daemon boundary: `syncd.py` imports no pipeline/workflow/HTTP
    modules — sync and spawn are the only outward calls (FR-4.4);
  * `external/harness_cli`: the spawn command line is
    `harness.py run-task-loop`, detached (no real child is started);
  * the run commands take `<workDir>/run.lock` and refuse (exit 1)
    while a live process holds it (FR-4.3);
  * `cmd_syncd` wiring: disabled config yields `sync=None`, enabled
    config wires the engine's full-pass dispatch.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from external.github_api import GitHubApiError  # noqa: E402
from external.harness_cli import spawn_harness_run_task_loop  # noqa: E402
from harness.cli import handlers  # noqa: E402
from harness.core.config import load  # noqa: E402
from harness.core.process_lock import (  # noqa: E402
    LockHolder,
    LockHeldError,
    ProcessLock,
    RUN_LOCK_NAME,
    SYNCD_LOCK_NAME,
)
from harness.core.syncd import (  # noqa: E402
    BACKOFF_MULTIPLIER,
    SYNC_FAILURE_THRESHOLD,
    SyncdLoop,
    SyncdParams,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _dead_pid() -> int:
    """A PID guaranteed not to be running: a reaped child."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class ProcessLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.lock = ProcessLock(self.work, "test.lock")

    def test_acquire_writes_pid_release_removes(self):
        self.lock.acquire()
        self.assertEqual(os.getpid(), int(self.lock.path.read_text().strip()))
        self.assertTrue(self.lock.is_held())
        self.lock.release()
        self.assertFalse(self.lock.path.exists())
        self.assertIsNone(self.lock.holder())
        self.lock.release()  # releasing an absent lock is fine

    def test_live_holder_refuses_acquire(self):
        self.lock.path.write_text(f"{os.getpid()}\n")
        with self.assertRaises(LockHeldError) as caught:
            self.lock.acquire()
        self.assertEqual(os.getpid(), caught.exception.holder.pid)
        self.assertTrue(self.lock.is_held())

    def test_dead_pid_is_stale_and_recovered(self):
        """AC-11 mechanism: a lock naming a dead PID is removed and taken."""
        self.lock.path.write_text(f"{_dead_pid()}\n")
        self.assertFalse(self.lock.is_held())
        self.lock.acquire()
        self.assertEqual(os.getpid(), int(self.lock.path.read_text().strip()))

    def test_corrupt_lock_is_stale_and_recovered(self):
        self.lock.path.write_text("not-a-pid\n")
        self.assertFalse(self.lock.is_held())
        self.lock.acquire()
        self.assertEqual(os.getpid(), int(self.lock.path.read_text().strip()))

    def test_clear_stale_file_refuses_a_live_lock_installed_under_it(self):
        """TOCTOU guard: the file changed to a fresh live holder between
        the stale read and the unlink — it must not be stolen."""
        self.lock.path.write_text(f"{os.getpid()}\n")
        with self.assertRaises(LockHeldError):
            self.lock._clear_stale_file(LockHolder(pid=_dead_pid()))
        self.assertTrue(self.lock.path.exists())

    def test_clear_stale_file_leaves_a_changed_stale_pid_alone(self):
        """A different stale PID appeared under recovery: leave it for the
        acquire loop to re-read rather than unlink the wrong file."""
        first, second = _dead_pid(), _dead_pid()
        while second == first:
            second = _dead_pid()
        self.lock.path.write_text(f"{first}\n")
        self.lock._clear_stale_file(LockHolder(pid=second))
        self.assertTrue(self.lock.path.exists())

    def test_clear_stale_file_removes_the_holder_it_expected(self):
        dead = _dead_pid()
        self.lock.path.write_text(f"{dead}\n")
        self.lock._clear_stale_file(LockHolder(pid=dead))
        self.assertFalse(self.lock.path.exists())


def _make_cfg(work: Path, **raw) -> object:
    """A real `Config` loaded from a temp config.json inside `work`."""
    cfg_path = work / "config.json"
    cfg_path.write_text(json.dumps({"workDir": str(work), **raw}))
    return load(cfg_path)


class SyncdLoopTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.logs: list[str] = []
        self.slept: list[float] = []

    def _sleep(self, seconds, stop):
        self.slept.append(seconds)

    def _loop(self, **overrides) -> SyncdLoop:
        params = dict(work_dir=self.work, sync_interval_s=10.0,
                      sync=lambda: None, spawn=lambda: 4242,
                      log=self.logs.append, sleep=self._sleep,
                      check_pending=lambda: False,
                      stop_after_passes=1)
        params.update(overrides)
        return SyncdLoop(SyncdParams(**params))

    def _lock_text(self, name: str) -> str | None:
        path = self.work / name
        return path.read_text() if path.exists() else None

    # -- AC-9: poll, sync, spawn one run ------------------------------------

    def test_pass_runs_sync_and_spawns_when_work_exists(self):
        sync_calls = []
        loop = self._loop(sync=lambda: sync_calls.append(1),
                          check_pending=lambda: True)
        self.assertEqual(0, loop.run())
        self.assertEqual(1, len(sync_calls))
        self.assertIn("spawned harness run pid 4242", "\n".join(self.logs))
        # The daemon itself never holds the run lock — the run does.
        self.assertIsNone(self._lock_text(RUN_LOCK_NAME))

    def test_no_spawn_when_pending_empty(self):
        spawns = []
        loop = self._loop(spawn=lambda: spawns.append(1) or 1,
                          check_pending=lambda: False)
        loop.run()
        self.assertEqual([], spawns)

    def test_no_spawn_when_run_lock_held_by_live_process(self):
        """FR-4.3: a hand-started run blocks spawning equally."""
        (self.work / RUN_LOCK_NAME).write_text(f"{os.getpid()}\n")
        spawns = []
        loop = self._loop(spawn=lambda: spawns.append(1) or 1,
                          check_pending=lambda: True)
        loop.run()
        self.assertEqual([], spawns)

    def test_no_second_spawn_while_child_alive(self):
        # The tracked child must be a real child of this process: liveness
        # is now probed with os.waitpid(WNOHANG), so a non-child pid (the
        # old os.getpid() fake) reads as dead via ChildProcessError.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)      # reap: runs after terminate (LIFO)
        self.addCleanup(child.terminate)  # do not block on the full sleep
        spawns = []
        loop = self._loop(spawn=lambda: spawns.append(1) or child.pid,
                          check_pending=lambda: True,
                          stop_after_passes=3)
        loop.run()
        self.assertEqual(1, len(spawns))

    def test_dead_child_is_reaped_and_spawning_resumes(self):
        """A spawned child that exits must not block spawning forever.

        The child is a real process, so the daemon is its parent: without a
        reap it stays a zombie, a liveness probe reads it as alive, and
        every later pass skips spawning.
        """
        children: list[subprocess.Popen] = []

        def spawn() -> int:
            # A real child of the test process that lives long enough to
            # be seen alive across the first run's passes, then exits.
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.5)"])
            children.append(child)
            return child.pid

        loop = self._loop(spawn=spawn, check_pending=lambda: True,
                          stop_after_passes=3)
        loop.run()
        # AC-2: the child stayed alive over all three passes — one spawn.
        self.assertEqual(1, len(children))
        first = children[0]
        self.addCleanup(first.wait)
        while loop._last_child_alive():
            # The child exits on its own; the loop reaps it in this call.
            time.sleep(0.01)
        self.assertFalse(loop._last_child_alive())
        self.assertIsNone(loop._spawned_pid)
        # The loop reaped it, not the test: the pid is no longer our child.
        with self.assertRaises(ChildProcessError):
            os.waitpid(first.pid, os.WNOHANG)
        # AC-1: the same instance spawns again on a later pass.
        loop.run()
        self.assertEqual(2, len(children))
        self.addCleanup(children[1].wait)
        self.assertNotEqual(first.pid, children[1].pid)

    def test_unconfigured_github_skips_sync_and_watches_local_work(self):
        """FR-0.1: sync=None — no sync runs, but the pending watch works."""
        spawns = []
        loop = self._loop(sync=None, spawn=lambda: spawns.append(1) or 7,
                          check_pending=lambda: True)
        self.assertEqual(0, loop.run())
        self.assertEqual(1, len(spawns))

    def test_spawn_failure_is_logged_and_retried_next_pass(self):
        calls = {"n": 0}

        def spawn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("fork failed")
            return 9

        loop = self._loop(spawn=spawn, check_pending=lambda: True,
                          stop_after_passes=2)
        self.assertEqual(0, loop.run())
        self.assertIn("spawn failed: OSError", "\n".join(self.logs))
        self.assertEqual(2, calls["n"])

    # -- AC-10 / AC-11: single instance --------------------------------------

    def test_second_daemon_exits_nonzero_with_lock_message(self):
        """AC-10: a live daemon holds `syncd.lock`."""
        (self.work / SYNCD_LOCK_NAME).write_text(f"{os.getpid()}\n")
        loop = self._loop()
        self.assertEqual(1, loop.run())
        text = "\n".join(self.logs)
        self.assertIn("another syncd is running", text)
        self.assertIn(str(os.getpid()), text)
        # The other daemon's lock survives the refused invocation.
        self.assertTrue((self.work / SYNCD_LOCK_NAME).exists())

    def test_stale_syncd_lock_is_recovered(self):
        """AC-11: a killed daemon's lock does not block the next one."""
        (self.work / SYNCD_LOCK_NAME).write_text(f"{_dead_pid()}\n")
        sync_calls = []
        loop = self._loop(sync=lambda: sync_calls.append(1))
        self.assertEqual(0, loop.run())
        self.assertEqual(1, len(sync_calls))
        self.assertIsNone(self._lock_text(SYNCD_LOCK_NAME))

    def test_daemon_lock_held_for_life_and_removed_after(self):
        seen = {}
        loop = self._loop(sync=lambda: seen.setdefault(
            "held", (self.work / SYNCD_LOCK_NAME).exists()))
        loop.run()
        self.assertTrue(seen["held"])
        self.assertFalse((self.work / SYNCD_LOCK_NAME).exists())

    # -- FR-4.5: failure backoff ---------------------------------------------

    def test_backoff_entry_reset_and_one_warning_per_entry(self):
        results = [False] * SYNC_FAILURE_THRESHOLD + [True] \
            + [False] * SYNC_FAILURE_THRESHOLD
        sync_calls = {"n": 0}

        def sync():
            sync_calls["n"] += 1
            if not results[sync_calls["n"] - 1]:
                raise RuntimeError("api down")

        loop = self._loop(sync=sync, stop_after_passes=len(results))
        self.assertEqual(0, loop.run())
        text = "\n".join(self.logs)
        # Exactly one backoff warning per backoff *entry* — two entries here.
        self.assertEqual(2, text.count("backing off"))
        # Intervals seen by sleep: base except after the 5th failure of
        # each streak (5x while in backoff).
        expected = [10.0] * 4 + [10.0 * BACKOFF_MULTIPLIER] + [10.0] * 4
        self.assertEqual(expected, self.slept[:len(expected)])
        # A success reset the counter: the second streak counts from 1/5.
        self.assertIn("(1/5)", text)

    def test_single_failure_does_not_back_off(self):
        calls = {"n": 0}

        def sync():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")

        loop = self._loop(sync=sync, stop_after_passes=3)
        loop.run()
        text = "\n".join(self.logs)
        self.assertNotIn("backing off", text)
        self.assertEqual([10.0, 10.0], self.slept)

    def test_aborted_report_counts_as_a_failed_pass(self):
        """FR-4.5 in production: the sync engine never raises on GitHub
        errors (spec edge 9) — it returns an aborted report — and that
        must engage the backoff."""
        report = SimpleNamespace(aborted=True,
                                 abort_reason="rate limit exhausted")
        loop = self._loop(sync=lambda: report,
                          stop_after_passes=SYNC_FAILURE_THRESHOLD + 1)
        self.assertEqual(0, loop.run())
        text = "\n".join(self.logs)
        self.assertIn("backing off", text)
        self.assertIn("rate limit exhausted", text)
        self.assertEqual(10.0 * BACKOFF_MULTIPLIER, self.slept[-1])

    def test_report_without_the_abort_flag_is_a_successful_pass(self):
        loop = self._loop(sync=lambda: SimpleNamespace(aborted=False),
                          stop_after_passes=3)
        loop.run()
        self.assertNotIn("failed", "\n".join(self.logs))
        self.assertEqual([10.0, 10.0], self.slept)

    # -- pending-work detection -------------------------------------------

    def test_sidecar_only_pending_directory_is_not_work(self):
        """A synced task's `.gh.json` sidecar stays in `pending/` after a
        claim moves the markdown (FR-1.6); it is not work and must never
        trigger a spawn — and neither must a hidden file."""
        pending = self.work / "queue" / "pending"
        pending.mkdir(parents=True)
        (pending / "x.md.gh.json").write_text("{}")
        (pending / ".hidden.md").write_text("not a task")
        spawns: list[int] = []
        loop = self._loop(check_pending=None,
                          spawn=lambda: spawns.append(1) or 4242,
                          stop_after_passes=2)
        self.assertEqual(0, loop.run())
        self.assertEqual([], spawns)
        (pending / "real.md").write_text("# work")
        loop = self._loop(check_pending=None,
                          spawn=lambda: spawns.append(1) or 4242,
                          stop_after_passes=1)
        self.assertEqual(0, loop.run())
        self.assertEqual([1], spawns)

    # -- FR-4.6: signals -------------------------------------------------------

    def test_stop_mid_run_finishes_pass_removes_lock_exits_zero(self):
        loop = self._loop(sync=lambda: loop.request_stop(),
                          stop_after_passes=None)
        self.assertEqual(0, loop.run())
        self.assertFalse((self.work / SYNCD_LOCK_NAME).exists())

    def test_sigterm_handler_requests_stop(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        loop = self._loop()
        handlers._register_syncd_signals(loop, self.logs.append)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(loop.stop_requested)
        self.assertIn("received", "\n".join(self.logs))


class DaemonBoundaryTest(unittest.TestCase):
    """FR-4.4: the daemon body only syncs and spawns — no pipeline."""

    def test_syncd_module_imports_nothing_from_the_pipeline(self):
        source = (_REPO_ROOT / "harness" / "core" / "syncd.py").read_text()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in imported:
            self.assertFalse(
                module.startswith("harness.workflow")
                or module in ("harness.core.session",
                              "harness.core.gitops",
                              "external.pi_cli", "external.git_cli",
                              "external.github_api"),
                f"syncd.py imports {module}; the daemon's only outward "
                "calls are the injected sync and spawn")

    def test_spawn_starts_harness_run_task_loop_detached(self):
        started = {}

        def fake_popen(argv, **kwargs):
            started["argv"] = argv
            started.update(kwargs)
            return SimpleNamespace(pid=98765)

        with mock.patch("external.harness_cli.subprocess.Popen",
                        new=fake_popen):
            pid = spawn_harness_run_task_loop()
        self.assertEqual(98765, pid)
        self.assertEqual("harness.py", Path(started["argv"][-2]).name)
        self.assertEqual("run-task-loop", started["argv"][-1])
        self.assertTrue(started["start_new_session"])


class RunLockTest(unittest.TestCase):
    """FR-4.3: run commands hold `<workDir>/run.lock` for their life."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.cfg = _make_cfg(self.work)
        self.logs: list[str] = []

    def test_context_takes_and_releases_the_lock(self):
        seen = {}
        with handlers._run_lock(self.cfg, self.logs.append) as acquired:
            seen["acquired"] = acquired
            seen["held"] = (self.work / RUN_LOCK_NAME).exists()
        self.assertTrue(seen["acquired"])
        self.assertTrue(seen["held"])
        self.assertFalse((self.work / RUN_LOCK_NAME).exists())

    def test_context_refuses_while_live_pid_holds(self):
        (self.work / RUN_LOCK_NAME).write_text(f"{os.getpid()}\n")
        with handlers._run_lock(self.cfg, self.logs.append) as acquired:
            self.assertFalse(acquired)
        self.assertIn("harness run refused", "\n".join(self.logs))

    def _refused_command(self, command) -> int:
        (self.work / RUN_LOCK_NAME).write_text(f"{os.getpid()}\n")
        runner = mock.Mock(spec=[])  # no validate_models attribute
        with mock.patch.object(handlers, "build",
                               return_value=(self.cfg, None, runner, None,
                                             None, self.logs.append)):
            return command()

    def test_cmd_run_refuses_when_run_lock_held(self):
        self.assertEqual(1, self._refused_command(handlers.cmd_run))
        self.assertIn("harness run refused", "\n".join(self.logs))

    def test_cmd_run_task_loop_refuses_when_run_lock_held(self):
        self.assertEqual(1, self._refused_command(
            handlers.cmd_run_task_loop))
        self.assertIn("harness run refused", "\n".join(self.logs))

    def test_cmd_run_task_refuses_when_run_lock_held(self):
        """FR-4.3: a hand-started single-task run blocks spawning equally,
        so it takes `run.lock` too and refuses while a live holder has it."""
        task = self.work / "task.md"
        task.write_text("# task\n")
        self.assertEqual(1, self._refused_command(
            lambda: handlers.cmd_run_task(str(task))))
        self.assertIn("harness run refused", "\n".join(self.logs))


class CmdSyncdWiringTest(unittest.TestCase):
    """`cmd_syncd` builds the loop from the composition root only."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.captured: list = []

        class CapturingLoop:
            def __init__(self, params):
                self.params = params
                CapturingLoop.captured.append(params)

            def request_stop(self):
                pass

            def run(self):
                return 0

        CapturingLoop.captured = self.captured
        self._loop_patch = mock.patch.object(handlers, "SyncdLoop",
                                             CapturingLoop)
        self._loop_patch.start()
        self.addCleanup(self._loop_patch.stop)

    def _run_syncd(self, **raw) -> SyncdParams:
        cfg = _make_cfg(self.work, **raw)
        log = lambda msg: None  # noqa: E731 - wiring test, output irrelevant
        with mock.patch.object(handlers, "build",
                               return_value=(cfg, None, None, None, None,
                                             log)):
            rc = handlers.cmd_syncd()
        self.assertEqual(0, rc)
        self.assertEqual(1, len(self.captured))
        return self.captured[0]

    def test_disabled_config_makes_sync_none(self):
        """FR-0.1/NFR-2: unconfigured GitHub — the daemon never syncs."""
        params = self._run_syncd()
        self.assertIsNone(params.sync)
        self.assertEqual(self.work, Path(params.work_dir))
        self.assertIs(params.spawn, spawn_harness_run_task_loop)

    def test_enabled_config_wires_the_full_pass_dispatch(self):
        engine = SimpleNamespace(on_stage_change=lambda task_id=None: None)
        cfg_raw = {"githubPat": "ghp_token", "githubRepo": "acme/widgets"}
        with mock.patch.object(handlers, "build_github_api",
                               return_value=object()), \
                mock.patch.object(handlers, "build_sync_engine",
                                  return_value=engine):
            params = self._run_syncd(**cfg_raw)
        self.assertIs(engine.on_stage_change, params.sync)

    def test_backoff_engages_through_the_real_sync_callable(self):
        """FR-4.5 with the production wiring: the engine's full-pass
        dispatch reports a persistently failing GitHub API as an aborted
        `SyncReport`, never an exception, and the real loop must count
        those passes and back off."""
        cfg_raw = {"githubPat": "ghp_token", "githubRepo": "acme/widgets"}
        with mock.patch.object(handlers, "build_github_api",
                               return_value=_FailingApi()):
            params = self._run_syncd(**cfg_raw)
        logs: list[str] = []
        slept: list[float] = []
        params = dataclasses.replace(
            params, log=logs.append, spawn=None,
            check_pending=lambda: False,
            sleep=lambda seconds, stop: slept.append(seconds),
            stop_after_passes=SYNC_FAILURE_THRESHOLD + 1)
        self.assertEqual(0, SyncdLoop(params).run())
        text = "\n".join(logs)
        self.assertIn("backing off", text)
        self.assertIn("aborted", text)
        self.assertEqual(params.sync_interval_s * BACKOFF_MULTIPLIER,
                         slept[-1])


class _FailingApi:
    """A fake GitHub client whose every read fails with an API error —
    the shape of a persistently down GitHub (rate limit, 500, auth)."""

    def reset_pass(self) -> None:
        pass

    def list_issues(self, *args, **kwargs):
        raise GitHubApiError("rate limit exhausted")


class CliSurfaceTest(unittest.TestCase):
    def test_parser_accepts_syncd(self):
        from harness.cli.parser import parse_args
        args = parse_args(["syncd"])
        self.assertEqual("syncd", args.command)


if __name__ == "__main__":
    unittest.main()
