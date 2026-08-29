"""T48: the streamed context cap in `external.pi_cli.run_pi_session`.

D2 stops a session the moment its context goes over the ceiling. Checking
`peak_tokens` after `run_pi_session()` returns is too late — the model keeps
consuming context for the rest of the session — so the check lives inside the
stdout event loop, on every `message_end` and `agent_end` usage value, and the
first value *strictly* over the cap terminates the child. The boundary is
asserted on both sides: 60,000 with a cap of 60,000 keeps running, 60,001 stops.

This module is named for that one behavior rather than folded into
`tests/test_pi_subprocess.py` (T35's file): the two leaves must be revertible
apart, so the fake-`pi` fixture is duplicated here instead of imported from a
sibling test module.

Only the stream layer is under test. Propagating the trip into `SessionResult`,
the stats row, the pipeline and the handoff are T49/T74/T75, so nothing here
imports `harness/`.

The over-cap cases need a child that is *still working* when the cap trips — a
child that exits on its own would prove nothing about termination. The fakes
therefore sleep or keep streaming after the over-cap event, and one of them
installs a SIGTERM handler so the marker file it writes proves the stop was a
terminate (the shared helper's contract) rather than a bare kill.

Deadlock protection follows T35: every case runs the session in a daemon worker
thread joined with `CASE_GUARD_S`. A child that blocks writing to a stdout pipe
nobody reads anymore is exactly the regression the over-cap path reintroduces
most easily, and it fails the case instead of hanging the suite.

The real `/usr/local/bin/pi` is never invoked: `setUp` asserts `shutil.which`
resolves to the fake inside the temp dir and skips loudly if it does not.

Run from the repo root:  python3 -m unittest tests.test_pi_over_cap_stream
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import external.pi_cli as P

# The configured ceiling (config.json `maxPromptTokens`). The values under test
# are the literal boundary, not scaled-down stand-ins: 60,000 must not trip and
# 60,001 must.
CAP = 60_000
OVER_CAP = 60_001

# Per-case wall guard. The longest legitimate case is the terminate one: the
# child sleeps for 600s, so a return at all is the stop working, and the budget
# is the shared helper's SIGTERM-then-SIGKILL grace plus the `finally` joins.
# Anything still running past this is a deadlock, not a slow session.
CASE_GUARD_S = 30

# Promptness bound for the termination case, asserted separately from the guard
# so a stop that works but takes minutes cannot pass.
TERMINATE_S = 15

# Names are fixed strings because the fake `pi` scripts interpolate them: each
# is a file the child writes to mark that it reached a point past the cap.
MARK_REACHED = "reached-after-cap.txt"
MARK_TERMINATED = "terminated-by-sigterm.txt"


def _message_end_event(text: str, total_tokens: int) -> str:
    """One `message_end` wire event carrying assistant text and a usage block."""
    return json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"totalTokens": total_tokens},
            "content": [{"type": "text", "text": text}],
        },
    })


def _agent_end_event(*total_tokens: int) -> str:
    """One `agent_end` wire event carrying one usage block per message."""
    return json.dumps({
        "type": "agent_end",
        "messages": [{"usage": {"totalTokens": n}} for n in total_tokens],
    })


def fake_pi(script_body: str, tmp: Path) -> None:
    """Write an executable `pi` into `tmp` whose body is `script_body`.

    The wrapper is a python3 script, so cases write python. `script_body` is
    dedented and wrapped in a `try/finally` that flushes both streams.

    stdout is switched to line buffering because that is what the real `pi`
    does — it streams JSON events as they happen. Against a pipe Python
    block-buffers by default, so a fake that printed an over-cap event and then
    slept would hold the event in its buffer until exit and the cap could never
    trip mid-run: the exact behavior under test would be unobservable.
    """
    body = textwrap.indent(textwrap.dedent(script_body).strip("\n"), "    ")
    (tmp / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys, time\n"
        "sys.stdout.reconfigure(line_buffering=True)\n"
        "try:\n"
        f"{body}\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (tmp / "pi").chmod(0o755)


class PiStreamContextCapTest(unittest.TestCase):
    """Real `Popen` against a fake `pi` on `PATH`. No model, no network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin_dir = Path(self._tmp.name) / "bin"
        self.bin_dir.mkdir()
        self.workdir = Path(self._tmp.name) / "work"
        self.workdir.mkdir()
        self.out_file = self.workdir / "s.out"

        # A stub `pi` exists before every case so the PATH assertion below can
        # never resolve to the real binary, even for a case that forgot to call
        # fake_pi (that case fails on its own assertion instead of spawning a
        # real model session).
        fake_pi("print('stub')", self.bin_dir)

        # Never leak a mutated PATH into other tests: save, mutate, restore.
        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))

        hard_timeout0 = P.HARD_TIMEOUT_S
        provider0 = os.environ.pop("HARNESS_PI_PROVIDER", None)
        self.addCleanup(lambda: setattr(P, "HARD_TIMEOUT_S", hard_timeout0))
        self.addCleanup(
            lambda: os.environ.__setitem__("HARNESS_PI_PROVIDER", provider0)
            if provider0 is not None
            else os.environ.pop("HARNESS_PI_PROVIDER", None))

        # Loud skip rather than a real session: `pi` must resolve inside the
        # temp dir. `Path.resolve` because temp dirs can be handed back
        # through a symlinked path (/var -> /private/var on macOS).
        found = shutil.which("pi")
        if found is None or (Path(found).resolve().parent
                             != self.bin_dir.resolve()):
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          f"refusing to run the real pi binary")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _mark(self, name: str) -> Path:
        """A marker file lives in the temp dir, not the workdir: the workdir is
        the child's cwd and the fake scripts are written with absolute paths."""
        return Path(self._tmp.name) / name

    def _run_session(self, *, max_context_tokens: int | None = CAP,
                     timeout_s: float = CASE_GUARD_S) -> P.PiSessionResult:
        """`run_pi_session` in a daemon worker, joined with a wall guard.

        The guard is the deadlock protection described in the module docstring:
        a wedged run fails this case instead of blocking the suite forever.
        """
        box: dict[str, object] = {}

        def worker():
            try:
                box["result"] = P.run_pi_session(
                    model="fake-model",
                    workdir=self.workdir,
                    prompt="p",
                    out_file=self.out_file,
                    log=lambda *a: None,
                    max_context_tokens=max_context_tokens,
                )
            except BaseException as exc:      # re-raised on the main thread
                box["error"] = exc

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout_s)
        if t.is_alive():
            self.fail(f"run_pi_session did not return within {timeout_s}s "
                      f"— deadlock regression (child still holding a pipe?)")
        if "error" in box:
            raise box["error"]                # type: ignore[misc]
        return box["result"]                  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # a. the boundary: exactly the cap keeps running
    # ------------------------------------------------------------------
    def test_usage_exactly_at_cap_does_not_trip(self):
        reached = self._mark(MARK_REACHED)
        fake_pi(f"""
            print({_message_end_event("still working ", CAP)!r})
            print({_agent_end_event(CAP)!r})
            print({_message_end_event("VERDICT: done", CAP - 1)!r})
            open({str(reached)!r}, 'w').write('finished normally')
        """, self.bin_dir)

        r = self._run_session(max_context_tokens=CAP)

        self.assertFalse(r.over_context_budget,
                         "exactly the cap is inside the cap")
        self.assertFalse(r.crashed)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.err, "")
        self.assertEqual(r.peak_tokens, CAP)
        self.assertEqual(r.context_limit, CAP)
        # Reading continued past the at-cap event: the later text is in the
        # output and the child ran to its own exit. The module joins streamed
        # text parts with "\n", so the first part's trailing space is kept.
        self.assertEqual(r.output, "still working \nVERDICT: done")
        self.assertTrue(reached.exists())

    # ------------------------------------------------------------------
    # b. one token over: trip, and the structured fields
    # ------------------------------------------------------------------
    def test_one_token_over_cap_trips_with_peak_limit_and_error(self):
        fake_pi(f"""
            print({_message_end_event("partial work ", 123)!r})
            print({_message_end_event("text after the trip", OVER_CAP)!r})
            print({_agent_end_event(90_000)!r})
        """, self.bin_dir)

        r = self._run_session(max_context_tokens=CAP)

        self.assertTrue(r.over_context_budget)
        self.assertEqual(r.peak_tokens, OVER_CAP,
                         "peak is the first over-cap value, not a later one")
        self.assertEqual(r.context_limit, CAP)
        # The error names both numbers, so an operator can tell the measured
        # peak from the ceiling that was in force.
        self.assertIn(P.OVER_CAP_ERR_PREFIX, r.err)
        self.assertIn(str(OVER_CAP), r.err)
        self.assertIn(str(CAP), r.err)
        # Text streamed before the trip survives; the over-cap message's does not.
        self.assertEqual(r.output, "partial work ")

    # ------------------------------------------------------------------
    # c. the trip is not a crash, and the child's rc is preserved
    # ------------------------------------------------------------------
    def test_over_cap_trip_is_not_reported_as_a_crash(self):
        fake_pi(f"""
            print({_message_end_event("over immediately", OVER_CAP)!r})
            sys.exit(0)
        """, self.bin_dir)

        r = self._run_session(max_context_tokens=CAP)

        self.assertTrue(r.over_context_budget)
        # `crashed` stays the crash channel: an over-cap stop must not be
        # indistinguishable from a dead child, and the generic "pi exited rc=N"
        # text must not claim the error field.
        self.assertFalse(r.crashed)
        self.assertNotIn("pi exited rc=", r.err)
        self.assertIsInstance(r.rc, int)

    # ------------------------------------------------------------------
    # d. `agent_end` usage is measured on the same rule
    # ------------------------------------------------------------------
    def test_over_cap_usage_on_agent_end_trips_too(self):
        fake_pi(f"""
            print({_message_end_event("working ", 1_000)!r})
            print({_agent_end_event(CAP, OVER_CAP)!r})
            time.sleep(600)
        """, self.bin_dir)

        r = self._run_session(max_context_tokens=CAP)

        self.assertTrue(r.over_context_budget,
                        "agent_end usage must be checked like message_end usage")
        self.assertEqual(r.peak_tokens, OVER_CAP)
        self.assertEqual(r.context_limit, CAP)
        self.assertIn(str(OVER_CAP), r.err)

    # ------------------------------------------------------------------
    # e. the child is terminated, not waited out
    # ------------------------------------------------------------------
    def test_over_cap_terminates_a_child_that_keeps_working(self):
        reached = self._mark(MARK_REACHED)
        terminated = self._mark(MARK_TERMINATED)
        # The handler is installed before the over-cap event is printed, so it
        # exists by the time the parser can trip. It is a one-liner on purpose:
        # `fake_pi` dedents/indents the body, so a multi-line block interpolated
        # into it would land at the wrong depth. `os.write` + `os._exit` are
        # unbuffered all the way down — a buffered `open().write()` followed by
        # `os._exit` could lose the marker.
        fake_pi(f"""
            signal.signal(signal.SIGTERM, lambda s, f: (os.write(os.open({str(terminated)!r}, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644), b'stopped'), os._exit(143)))
            print({_message_end_event("over the line", OVER_CAP)!r})
            time.sleep(600)
            open({str(reached)!r}, 'w').write('never')
        """, self.bin_dir)

        t0 = time.monotonic()
        r = self._run_session(max_context_tokens=CAP, timeout_s=CASE_GUARD_S)
        elapsed = time.monotonic() - t0

        self.assertTrue(r.over_context_budget)
        self.assertLess(elapsed, TERMINATE_S,
                        f"child was not terminated promptly ({elapsed:.0f}s) — "
                        f"the run waited out the child instead of stopping it")
        self.assertFalse(reached.exists(),
                         "child kept working past the cap instead of being stopped")
        # The marker the child writes from its own SIGTERM handler: proof the
        # stop went through terminate (the shared helper), not a bare kill, and
        # that the child really died rather than being abandoned.
        self.assertTrue(terminated.exists(),
                        "child never received SIGTERM from the stop path")

    # ------------------------------------------------------------------
    # f. no deadlock: a child still filling the stdout pipe after the trip
    # ------------------------------------------------------------------
    def test_over_cap_with_child_still_streaming_does_not_deadlock(self):
        # After the trip we stop reading stdout, so a child that keeps writing
        # blocks once the ~64 KB pipe buffer is full. Only stopping the child
        # releases it; waiting on it first would hang the reap forever.
        fake_pi(f"""
            print({_message_end_event("over the line", OVER_CAP)!r})
            line = 'x' * 200
            for i in range(40000):
                print(line)
        """, self.bin_dir)

        t0 = time.monotonic()
        r = self._run_session(max_context_tokens=CAP, timeout_s=CASE_GUARD_S)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, TERMINATE_S,
                        f"deadlocked on the stdout pipe ({elapsed:.0f}s)")
        self.assertTrue(r.over_context_budget)
        self.assertEqual(r.peak_tokens, OVER_CAP)

    # ------------------------------------------------------------------
    # g. no cap configured: the parameter is opt-in
    # ------------------------------------------------------------------
    def test_without_a_cap_no_usage_value_trips(self):
        huge = 200_000
        fake_pi(f"""
            print({_message_end_event("long session ", huge)!r})
            print({_agent_end_event(huge)!r})
        """, self.bin_dir)

        r = self._run_session(max_context_tokens=None)

        self.assertFalse(r.over_context_budget)
        self.assertFalse(r.crashed)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.err, "")
        self.assertEqual(r.peak_tokens, huge)
        self.assertIsNone(r.context_limit)


if __name__ == "__main__":
    unittest.main()
