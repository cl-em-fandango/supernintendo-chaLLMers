"""T35: subprocess tests for `external/pi_cli.run_pi_session` (finding F11).

`run_pi_session` is the only place the harness talks to the model, and it is the
part of the codebase that has been rewritten twice inside this plan (T17's stderr
drain, T18's wall-clock watchdog) with nothing but a hand-run snippet to prove
either change. Both of those failure modes are invisible to a pure-text test: a
child that fills the ~64 KB stderr pipe buffer stalls the run forever, and a
child that prints nothing never reaches the in-loop deadline check. So the
subjects here are real `Popen` processes driven by a *fake* `pi` shell script
placed first on `PATH`, which is also what makes them runnable without a model.

The two verify snippets from T17 and T18 are promoted verbatim into
`test_stderr_flood…` and `test_silent_child…` so the next refactor of that
function cannot silently reintroduce a deadlock or an unbounded hang.

Verdict *parsing* is not tested here — that is T34's pure table
(`tests/test_pi_verdict.py`). Case (e) only asserts what the subprocess layer
does with unparseable stdout, and reads `_extract_verdict` to pin the
consequence (`unknown`) rather than re-testing the regex.

Deadlock protection: every case runs the session inside a daemon worker thread
and joins it with a per-case `CASE_GUARD_S` deadline (see `_run_session`). A
regression that reintroduces the pipe deadlock fails the case instead of hanging
the suite — a `threading.Timer(…, os._exit)` would kill the whole run and hide
which case hung, and a `subprocess`-level timeout is impossible because the
function under test owns the `Popen`. The worker is a daemon so a wedged child
cannot keep the interpreter alive at exit either.

The real `/usr/local/bin/pi` is never invoked: `setUp` asserts `shutil.which`
resolves to the fake inside the temp dir and skips loudly if it does not.

Run from the repo root:  python3 -m unittest tests.test_pi_subprocess
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
from external.pi_cli import _extract_verdict

# Per-case wall guard. The longest legitimate case is the silent-child one:
# monkeypatched HARD_TIMEOUT_S (2s) + the watchdog's kill-then-reap grace + the
# finally's joins. Anything still running after this is a deadlock, not a slow
# session, and must fail rather than hang CI.
CASE_GUARD_S = 45

# 200 KB on stderr: past the ~64 KB OS pipe buffer, which is the number that
# matters — below it the T17 deadlock cannot reproduce.
STDERR_FLOOD_LINES = 2000
STDERR_FLOOD_MIN_CHARS = 200_000


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


def _agent_end_event(total_tokens: int) -> str:
    """One `agent_end` wire event carrying a usage block."""
    return json.dumps({
        "type": "agent_end",
        "messages": [{"usage": {"totalTokens": total_tokens}}],
    })


def fake_pi(script_body: str, tmp: Path) -> None:
    """Write an executable `pi` into `tmp` whose body is `script_body`.

    The wrapper is a python3 script, so cases write python. `script_body` is
    dedented and wrapped in a `try/finally` that flushes both streams: a case
    that writes stdout and then exits (the crash case) must not have its output
    lost to block buffering.
    """
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


class PiSubprocessTest(unittest.TestCase):
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

        # Same discipline for the module constants the cases monkeypatch.
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
    def _run_session(self, timeout_s: float = CASE_GUARD_S) -> P.PiSessionResult:
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
    # a. clean session
    # ------------------------------------------------------------------
    def test_clean_session_returns_output_peak_tokens_and_out_file(self):
        fake_pi(f"""
            print({_message_end_event("all good VERDICT: done", 123)!r})
            print({_agent_end_event(200)!r})
        """, self.bin_dir)

        r = self._run_session()

        self.assertEqual(r.rc, 0)
        self.assertFalse(r.crashed)
        self.assertEqual(r.err, "")
        self.assertEqual(r.output, "all good VERDICT: done")
        self.assertEqual(r.peak_tokens, 200)   # max(message_end, agent_end)
        self.assertTrue(r.out_file.exists())
        self.assertEqual(r.out_file.read_text(), "all good VERDICT: done")
        self.assertEqual(_extract_verdict(r.output), "done")
        # A clean run writes no stderr side file.
        self.assertFalse(r.out_file.with_suffix(".out.err").exists())

    # ------------------------------------------------------------------
    # b. T17 regression: stderr flood must not deadlock, must not reach output
    # ------------------------------------------------------------------
    def test_stderr_flood_does_not_deadlock_and_stays_out_of_output(self):
        fake_pi(f"""
            for i in range({STDERR_FLOOD_LINES}):
                print('noisy stderr line %d ' % i * 8, file=sys.stderr)
            print({_message_end_event("all good VERDICT: done", 123)!r})
        """, self.bin_dir)

        t0 = time.monotonic()
        r = self._run_session(timeout_s=30)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 30, "deadlocked on the stderr pipe")
        self.assertEqual(r.rc, 0)
        self.assertFalse(r.crashed)
        self.assertIn("noisy stderr line", r.stderr)
        self.assertGreater(len(r.stderr), STDERR_FLOOD_MIN_CHARS,
                           "stderr not captured in full")
        # The T17 bug: stderr spliced into `output` let the child's own stderr
        # text fabricate a verdict.
        self.assertNotIn("[stderr]", r.output)
        self.assertNotIn("noisy", r.output)
        self.assertEqual(_extract_verdict(r.output), "done")
        # Operator-side copy of stderr exists as one file per session.
        self.assertTrue(r.out_file.with_suffix(".out.err").exists())

    # ------------------------------------------------------------------
    # c. T18 regression: silent child is killed by the wall-clock watchdog
    # ------------------------------------------------------------------
    def test_silent_child_hits_wall_clock_watchdog(self):
        fake_pi("""
            import time
            time.sleep(999)      # silent on both streams: no line ever arrives
        """, self.bin_dir)
        P.HARD_TIMEOUT_S = 2     # monkeypatched clock, real value restored in setUp

        t0 = time.monotonic()
        r = self._run_session(timeout_s=20)
        elapsed = time.monotonic() - t0

        # HARD_TIMEOUT_S + the watchdog's kill-then-reap grace + the joins.
        self.assertLess(elapsed, P.HARD_TIMEOUT_S + 15,
                        f"watchdog did not fire (waited {elapsed:.0f}s)")
        self.assertTrue(r.crashed, "a wall-clock timeout must be a crash")
        self.assertIn("wall-clock timeout", r.err)
        self.assertIn("after 2s", r.err)

    # ------------------------------------------------------------------
    # d. nonzero exit
    # ------------------------------------------------------------------
    def test_nonzero_exit_is_a_crash_with_err(self):
        fake_pi("""
            print('partial answer')
            sys.exit(3)
        """, self.bin_dir)

        r = self._run_session()

        self.assertEqual(r.rc, 3)
        self.assertTrue(r.crashed)
        self.assertTrue(r.err.strip())
        self.assertIn("rc=3", r.err)

    # ------------------------------------------------------------------
    # e. stdout that is not JSON at all
    # ------------------------------------------------------------------
    def test_non_json_stdout_yields_empty_output_and_unknown_verdict(self):
        fake_pi("""
            print('plain prose, no JSON anywhere on this line')
            print('neither is this one')
        """, self.bin_dir)

        r = self._run_session()

        # Asserted, not changed: every stdout line fails json.loads and is
        # skipped, so the subprocess layer returns an empty session with rc 0.
        self.assertEqual(r.rc, 0)
        self.assertFalse(r.crashed)
        self.assertEqual(r.output, "")
        self.assertEqual(r.peak_tokens, 0)
        self.assertEqual(_extract_verdict(r.output), "unknown")
        self.assertEqual(self.out_file.read_text(), "")

    # ------------------------------------------------------------------
    # f. malformed lines interleaved with good ones
    # ------------------------------------------------------------------
    def test_malformed_json_lines_do_not_escape_and_good_lines_still_parse(self):
        # Junk the parser has to swallow: prose, truncated JSON, whitespace, and
        # JSON that parses but is not an event object at all.
        junk = [
            "not json at all",
            '{"type": "message_end", broken json',
            "   ",
            "[]",
            "null",
            '{"type": "message_end"}',
        ]
        lines = [junk[0],
                 _message_end_event("first half ", 123),
                 junk[1], junk[2], junk[3], junk[4], junk[5],
                 _message_end_event("VERDICT: done", 999),
                 _agent_end_event(456)]
        body = "\n".join(f"print({json.dumps(line)!r})" for line in lines)
        fake_pi(body, self.bin_dir)

        r = self._run_session()

        self.assertEqual(r.rc, 0)
        self.assertFalse(r.crashed)
        # Both good message_end texts survive in order (joined with "\n" by the
        # module, so the trailing space of the first one is part of the text); the
        # junk lines are dropped.
        self.assertEqual(r.output, "first halfVERDICT: done")
        # The empty `{"type": "message_end"}` line parses but carries no usage,
        # so it contributes nothing rather than raising on a missing key.
        self.assertEqual(r.peak_tokens, 999)
        self.assertEqual(_extract_verdict(r.output), "done")


if __name__ == "__main__":
    unittest.main()
