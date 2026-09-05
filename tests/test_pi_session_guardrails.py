"""Slice 4.2: the pi session path routes through the hardened runner.

`external/pi_cli.run_pi_session` now takes an explicit `timeout_s` (the config
key `sessionTimeout`, handed down by `harness/core/session.py`), spawns pi via
the hardened runner's `spawn()` (own session, so the stop is a group stop),
and reports a timed-out session as a crash carrying the `wall-clock timeout`
prefix — never silently swallowed (FR-4.1).

Cases a/b drive a *fake* `pi` shell script placed first on `PATH` (the T35
pattern in `tests/test_pi_subprocess.py`): no model, no network, and the real
`pi` binary is refused by the `setUp` assertion. Every case runs inside a
daemon worker with a wall guard so a regression fails instead of hanging.

Case c proves the wiring at the `SessionRunner` level with a double — no
subprocess at all.

Run from the repo root:  python3 -m unittest tests.test_pi_session_guardrails
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import external.pi_cli as P
from external.pi_cli import PiSessionResult, run_pi_session
from harness.core.config import Config
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore

CASE_GUARD_S = 30


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
        return status[status.rfind(")") + 1:].split()[0] != "Z"
    except (FileNotFoundError, IndexError, PermissionError):
        return True


class PiTimeoutTest(unittest.TestCase):
    """A real fake-`pi` child under the configurable wall-clock cap."""

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

        found = shutil.which("pi")
        if found is None or (Path(found).resolve().parent
                             != self.bin_dir.resolve()):
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          f"refusing to run the real pi binary")

    def _run(self, *, timeout_s: float) -> PiSessionResult:
        box: dict[str, object] = {}

        def worker():
            try:
                box["result"] = run_pi_session(
                    model="fake-model",
                    workdir=self.workdir,
                    prompt="p",
                    out_file=self.out_file,
                    log=lambda *a: None,
                    timeout_s=timeout_s,
                )
            except BaseException as exc:
                box["error"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=CASE_GUARD_S)
        if t.is_alive():
            self.fail("run_pi_session did not return within the wall guard "
                      "— the timeout guardrail regressed")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["result"]  # type: ignore[return-value]

    def test_timeout_s_overrides_the_hard_constant_and_is_a_crash(self):
        fake_pi("""
            import time
            time.sleep(999)      # silent: only the watchdog can end this
        """, self.bin_dir)

        t0 = time.monotonic()
        r = self._run(timeout_s=1)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 15, "timeout_s=1 did not stop the child")
        self.assertTrue(r.crashed, "a timed-out session must be a crash")
        self.assertIn("wall-clock timeout", r.err)
        self.assertIn("after 1s", r.err,
                      "the message must carry the configured cap, not 5400")

    def test_timed_out_pi_takes_its_tool_child_down_as_a_group(self):
        pid_file = self.workdir / "grandchild.pid"
        fake_pi(f"""
            import subprocess, time
            child = subprocess.Popen(["sleep", "60"])   # pi's "tool" child
            open({str(pid_file)!r}, "w").write(str(child.pid))
            time.sleep(999)
        """, self.bin_dir)

        r = self._run(timeout_s=1)

        self.assertTrue(r.crashed)
        self.assertIn("wall-clock timeout", r.err)
        grandchild = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_alive(grandchild):
            time.sleep(0.05)
        self.assertFalse(
            _pid_alive(grandchild),
            f"pi's tool child pid {grandchild} outlived the group kill",
        )


class SessionTimeoutPropagationTest(unittest.TestCase):
    """`SessionRunner` hands the config `sessionTimeout` down to pi_cli."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()

    def _cfg(self, raw_extra: dict) -> Config:
        raw = {"harnessExecutionAndQueueDir": str(self.work_dir),
               "maxPromptTokens": 60_000,
               "models": {"technicalWriter": "m", "implementer": "m",
                          "assessor": "m"}}
        raw.update(raw_extra)
        return Config(
            harness_execution_and_queue_dir=self.work_dir,
            token_budget=60_000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=5,
            trunk_branch="pi/trunk",
            task_provider="directory",
            directory_provider={},
            models=raw["models"],
            model_context_map={},
            raw=raw,
        )

    def _run_with_double(self, cfg: Config) -> list[dict]:
        calls: list[dict] = []

        def pi_double(*, model, workdir, prompt, out_file, log,
                      max_context_tokens=None, timeout_s=None):
            calls.append({"timeout_s": timeout_s})
            Path(out_file).write_text("VERDICT: done")
            return PiSessionResult(rc=0, crashed=False, err="", peak_tokens=7,
                                   duration_s=0.1, output="VERDICT: done",
                                   out_file=Path(out_file), stderr="")

        runner = SessionRunner(cfg, StatsStore(cfg.stats_path),
                               log=lambda *a: None)
        with patch("harness.core.session.run_pi_session", pi_double):
            runner.run("m", self.work_repo, "p", task_id="t1")
        return calls

    def test_configured_session_timeout_reaches_pi_cli(self):
        calls = self._run_with_double(self._cfg({"sessionTimeout": 1234}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout_s"], 1234)

    def test_default_session_timeout_is_3600(self):
        calls = self._run_with_double(self._cfg({}))
        self.assertEqual(calls[0]["timeout_s"], 3600)


if __name__ == "__main__":
    unittest.main()
