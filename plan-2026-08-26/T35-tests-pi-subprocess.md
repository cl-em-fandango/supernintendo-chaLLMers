# T35 — Subprocess tests for `pi_cli`: stderr, watchdog, crash, with a fake `pi`

**Wave 9** · depends: T34, T17, T18 · finding: F11

## Context
`run_pi_session` is the only place the harness talks to the model, and it is untested (F11). It has
already been rewritten twice in this plan (T17 stderr drain, T18 watchdog) with nothing but a
hand-run snippet to prove it. The pieces are testable without a model: put a **fake `pi`** shell
script on `PATH` that prints/doesn't print/sleeps/writes stderr, and drive the real `Popen`. This
card turns the two ad-hoc verify snippets from T17 and T18 into permanent tests so the next refactor
of that function cannot silently reintroduce a deadlock.

## Read first
- `external/pi_cli.py` — `run_pi_session` end to end, `PiSessionResult`, `HEARTBEAT_S`,
  `HARD_TIMEOUT_S`, the drain and watchdog threads
- `plan-2026-08-26/T17-stderr-drain.md` and `T18-wallclock-watchdog.md` — their verify blocks are
  literally the tests to promote
- `tests/test_continue_fresh.py` — house style for temp dirs and cleanup

## Do
1. New file `tests/test_pi_subprocess.py`. A helper `def fake_pi(script_body, tmp) -> None` writes an
   executable `pi` into a temp dir and prepends it to `os.environ["PATH"]` in `setUp`, restoring in
   `tearDown` (never leak a mutated `PATH` into other tests — use `addCleanup`).
2. Cases, each with its own timeout guard so a regression fails instead of hanging CI:
   **a.** clean session, one `message_end` + `agent_end` → `rc == 0`, `crashed False`, `output` holds
   the assistant text, `peak_tokens` parsed, `out_file` written.
   **b.** 200 KB on stderr, small stdout → returns in well under 30 s, `result.stderr` populated,
   `"[stderr]" not in result.output` (the T17 regression test).
   **c.** silent child (`time.sleep(999)`) with `HARD_TIMEOUT_S` monkeypatched to 2 → returns in
   `HARD_TIMEOUT_S + grace`, `crashed True`, `"wall-clock timeout"` in `err` (T18's test).
   **d.** nonzero exit (`sys.exit(3)`) → `rc == 3`, `crashed True`, `err` non-empty.
   **e.** stdout with no JSON at all → `rc == 0`, `output == ""` (or whatever the code does — assert
   it, don't change it), verdict `unknown`.
   **f.** malformed JSON lines mixed with good ones → good lines still parse, no exception escapes.
3. Monkeypatch module constants (`P.HARD_TIMEOUT_S = 2`) rather than editing them; restore in
   `tearDown`.
4. Wrap every case in `threading.Timer(45, ...)`-style protection **or** a `subprocess` level timeout
   so a deadlock cannot hang the suite forever. Say in a comment which you chose.
5. **Never** invoke the real `/usr/local/bin/pi`: assert the fake is first on `PATH` at the top of
   `setUp` (`shutil.which("pi")` must resolve inside the temp dir) and skip loudly if it is not.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, unittest, time, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_pi_subprocess.py'); assert p.exists()
t0 = time.monotonic()
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_pi_subprocess')
assert suite.countTestCases() >= 5, "need the six cases (or >=5 plus one merged)"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
assert time.monotonic() - t0 < 120, f"too slow ({time.monotonic()-t0:.0f}s) — a case is waiting on a real timeout"
print(f"pi subprocess tests ok in {time.monotonic()-t0:.1f}s")
PY
```
Must pass, plus the Gate.

## Out of scope
Verdict *parsing* tables (T34), `SessionRunner`'s verdict→stats mapping (T20, tested in T34's pure
layer), the over-cap trip (T42), anything that runs a **real** model session or touches
`--provider llama-swap`, `supervisor.py`'s own child handling (T38), and network access of any kind.

## Done when
`tests/test_pi_subprocess.py` has ≥5 cases including the stderr-flood and silent-child cases; the
whole file runs in under 2 minutes; `shutil.which("pi")` inside the test resolves to the fake; the
suite passes with the real `pi` never executed.
