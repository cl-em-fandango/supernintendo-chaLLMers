"""FR-5.1 sub-slice 2.3: the pre-flight gates session dispatch.

`Pipeline._run` probes the model server before *every* `runner.run` call
(including before a crash retry — the server may have died between
attempts). Pinned here:

- unreachable endpoint: `_run` backs off (observed sleeps, greppable
  lines) and raises `ServerUnhealthy` — the runner is never called, so
  no crash-retry attempt is spent and `max_crash_retries` is untouched;
- no endpoint configured: dispatch is identical to today — the runner is
  called, the result returned, and no `LLM-HEALTH` line is emitted
  (NFR-2 regression);
- healthy endpoint: dispatch proceeds immediately;
- `process` catches `ServerUnhealthy` once and parks with the reason,
  distinct from the crash path, with the task resumable.

No subprocess, no container, no live model server: a closed localhost
port stands in for the dead server, an in-process responder for a live
one, and the backoff clock is squeezed to milliseconds (NFR-4).

Run from the repo root:  python3 -m unittest tests.test_health_dispatch_preflight
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage, Verdict
from harness.core.health import HealthGate, HealthOutcome, wait_for_healthy_server
from harness.core.providers import Task
from harness.core.session import SessionResult
from harness.workflow.pipeline import Pipeline, ServerUnhealthy
from harness.workflow.task_lifecycle import TaskLifecycle

from tests.test_health_probe import _Responder, _closed_port_url


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _cfg(work_dir: Path, raw: dict | None = None) -> Config:
    return Config(
        work_dir=work_dir,
        token_budget=100_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
        model_context_map={},
        raw=raw or {},
    )


def _health_raw(url: str) -> dict:
    """Unreachable-endpoint config with millisecond backoff so the real
    wait runs without costing the test suite wall-clock time."""
    return {
        "llmHealthUrl": url,
        "llmHealthTimeoutS": 0.5,
        "llmHealthMaxAttempts": 3,
        "llmHealthBackoffBaseS": 0.01,
        "llmHealthBackoffCapS": 0.02,
    }


class RecordingRunner:
    """Stands in for `SessionRunner`: records calls, always returns healthy."""

    def __init__(self):
        self.calls: list = []

    def run(self, model, workdir, prompt, *, task_id=None, stage=None, **kw):
        self.calls.append(stage)
        output = f"## Summary\nscripted\n\nVERDICT: {Verdict.DONE.value}"
        out_file = Path(workdir) / f".pi-session-{len(self.calls)}.out"
        out_file.write_text(output)
        return SessionResult(ok=True, verdict=Verdict.DONE, peak_tokens=0,
                             duration_s=0.0, output=output, out_file=out_file)


class RunPreflightTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.lines: list[str] = []
        self.runner = RecordingRunner()

    def _pipeline(self, raw: dict | None = None, health_wait=None) -> Pipeline:
        kwargs = {} if health_wait is None else {"health_wait": health_wait}
        return Pipeline(_cfg(self.work_dir, raw), self.runner,
                        log=self.lines.append, **kwargs)

    def _run(self, pipeline: Pipeline) -> SessionResult:
        return pipeline._run("m", self.work_repo, "prompt", task_id="t1",
                             stage=Stage.SPEC_AUTHOR)

    # ---------------- unreachable endpoint ----------------

    def test_unreachable_server_raises_server_unhealthy_not_a_crash(self):
        pipeline = self._pipeline(_health_raw(_closed_port_url()))
        with self.assertRaises(ServerUnhealthy) as caught:
            self._run(pipeline)
        exc = caught.exception
        self.assertEqual(exc.task_id, "t1")
        self.assertIs(exc.stage, Stage.SPEC_AUTHOR)
        self.assertEqual(exc.attempts, 3)
        self.assertGreater(exc.waited_s, 0.0,
                           "the backoff sleeps actually happened")
        self.assertIn("LLM server unhealthy", str(exc))

    def test_unhealthy_wait_spends_no_crash_retry_attempt(self):
        """The runner was never called: a crash retry is a `runner.run`
        attempt, and the pre-flight spent none of them."""
        pipeline = self._pipeline(_health_raw(_closed_port_url()))
        self.assertEqual(pipeline.max_crash_retries, 2)
        with self.assertRaises(ServerUnhealthy):
            self._run(pipeline)
        self.assertEqual(self.runner.calls, [],
                         "a known-unhealthy server must not be dispatched to")
        self.assertEqual(pipeline.max_crash_retries, 2,
                         "the crash-retry budget is untouched")

    def test_backoff_waits_are_logged_greppably(self):
        pipeline = self._pipeline(_health_raw(_closed_port_url()))
        with self.assertRaises(ServerUnhealthy):
            self._run(pipeline)
        joined = "\n".join(self.lines)
        self.assertEqual(joined.count("LLM-HEALTH-BACKOFF"), 2,
                         "one line per wait, none after the last probe")
        self.assertIn("LLM-HEALTH-EXHAUSTED", joined)

    def test_injected_gate_outcome_also_blocks_the_dispatch(self):
        """The injectable seam: a gate reporting UNHEALTHY stops the run
        even when the configured endpoint would be reachable."""
        gate = HealthGate(HealthOutcome.UNHEALTHY, attempts=5, total_wait_s=1.5)
        pipeline = self._pipeline(health_wait=lambda policy, **kw: gate)
        with self.assertRaises(ServerUnhealthy):
            self._run(pipeline)
        self.assertEqual(self.runner.calls, [])

    # ---------------- disabled / healthy ----------------

    def test_no_endpoint_configured_dispatch_is_unchanged(self):
        """NFR-2 regression: with no health config the run behaves exactly
        as before FR-5.1 — one runner call, result returned, no new log
        lines of any kind from the gate."""
        pipeline = self._pipeline(raw={})
        result = self._run(pipeline)
        self.assertTrue(result.ok)
        self.assertEqual(self.runner.calls, [Stage.SPEC_AUTHOR])
        joined = "\n".join(self.lines)
        self.assertNotIn("LLM-HEALTH", joined)
        self.assertEqual(self.lines, [],
                         "the disabled pre-flight must add no log line")

    def test_disabled_flag_with_url_present_is_also_a_noop(self):
        pipeline = self._pipeline({"llmHealthUrl": _closed_port_url(),
                                   "llmHealthEnabled": False})
        result = self._run(pipeline)
        self.assertTrue(result.ok)
        self.assertEqual(len(self.runner.calls), 1)

    def test_healthy_server_proceeds_immediately(self):
        responder = _Responder(status=200)
        self.addCleanup(responder.shutdown)
        pipeline = self._pipeline(_health_raw(responder.url))
        result = self._run(pipeline)
        self.assertTrue(result.ok)
        self.assertEqual(self.runner.calls, [Stage.SPEC_AUTHOR])
        joined = "\n".join(self.lines)
        self.assertNotIn("LLM-HEALTH-BACKOFF", joined,
                         "a healthy first probe must not wait")

    def test_preflight_runs_before_every_attempt_including_crash_retries(self):
        """The server dies between attempts: the retry's pre-flight raises
        instead of dispatching, and the crash path never sees a dispatch."""
        gates = iter([
            HealthGate(HealthOutcome.HEALTHY, 1, 0.0),    # first dispatch ok
            HealthGate(HealthOutcome.UNHEALTHY, 3, 0.5),  # server died
        ])
        pipeline = self._pipeline(health_wait=lambda policy, **kw: next(gates))

        def crashing_run(model, workdir, prompt, *, task_id=None, stage=None,
                         **kw):
            self.runner.calls.append(stage)
            out_file = Path(workdir) / ".pi-session-crash.out"
            out_file.write_text("dead")
            return SessionResult(ok=False, verdict=Verdict.ERROR,
                                 peak_tokens=0, duration_s=0.0, output="",
                                 out_file=out_file, crashed=True)

        self.runner.run = crashing_run
        with self.assertRaises(ServerUnhealthy):
            self._run(pipeline)
        self.assertEqual(len(self.runner.calls), 1,
                         "the crashed attempt ran; the retry was gated off "
                         "before dispatch, not counted as a second crash")


class ProcessCatchSiteTest(unittest.TestCase):
    """`process` catches `ServerUnhealthy` once and parks — resumably."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)
        self.repo = self._make_repo(self.work_dir / "repo")
        self.cfg = _cfg(self.work_dir)
        self.cfg.repo_dir = self.repo
        self.lines: list[str] = []
        self.runner = RecordingRunner()

    def _make_repo(self, root: Path) -> Path:
        root.mkdir(parents=True)
        (root / "README.md").write_text("work target\n")
        _git(root, "init", "-b", "pi/trunk")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        return root

    def _task(self) -> Task:
        return Task(id="t1", body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def test_unhealthy_server_parks_once_with_the_reason(self):
        gate = HealthGate(HealthOutcome.UNHEALTHY, attempts=3, total_wait_s=1.5)
        pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append,
                            health_wait=lambda policy, **kw: gate)
        parks: list[tuple[str, str]] = []
        real_park = pipeline.lifecycle.park

        def spy(task_id: str, reason: str) -> None:
            parks.append((task_id, reason))
            real_park(task_id, reason)

        pipeline.lifecycle.park = spy
        status = pipeline.process(self._task())
        self.assertEqual(status, "parked")
        self.assertEqual(len(parks), 1)
        reason = parks[0][1]
        self.assertIn("LLM server unhealthy", reason)
        self.assertIn("spec_author", reason)
        self.assertNotIn("attempts crashed", reason,
                         "the unhealthy outcome must not read as a crash")
        self.assertEqual(self.runner.calls, [],
                         "no session was ever dispatched")
        parked_json = self.queue_dir / "parked" / "t1" / "task.json"
        self.assertTrue(parked_json.exists())
        self.assertEqual(json.loads(parked_json.read_text())["status"],
                         "parked")


if __name__ == "__main__":
    unittest.main()
