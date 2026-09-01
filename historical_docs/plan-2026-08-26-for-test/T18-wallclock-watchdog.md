# T18 — Kill a silent pi session with a wall-clock watchdog

**Wave 4** · depends: T17 · finding: F5

## Context
`external/pi_cli.py` computes `deadline = t0 + HARD_TIMEOUT_S` (`HARD_TIMEOUT_S = 5400`) but only
tests it **inside** `for line in proc.stdout:`. A blocked `read()` yields no line, so a child that
prints nothing never trips the deadline: the run hangs forever with no rc, no log line and no stats
row. After T17 the stdout loop ends cleanly once the child dies, so a `proc.kill()` is now enough to
unblock it. This is the second half of "every downstream finding is untrustworthy until the session
either returns or dies on a clock we control".

## Read first
- `external/pi_cli.py` — `run_pi_session` (Popen block, the stdout loop with its deadline check, the
  `finally` that clears the heartbeat stop event, the `PiSessionResult` construction)
- `harness/core/session.py` — how `crashed` and `err` feed stats `notes` and the verdict
- `plan-2026-08-26/T17-stderr-drain.md` — the thread shape and the `stderr` field you are building on

## Do
1. Add a module constant `WATCHDOG_GRACE_S = 5` (kill-then-reap grace) next to `HARD_TIMEOUT_S`.
2. After `Popen`, start a **daemon** watchdog thread: loop `while proc.poll() is None:` and
   `remaining = deadline - time.monotonic()`; if `remaining <= 0` → `proc.kill()` and break; else
   `time.sleep(min(1.0, remaining))`. Sub-second sleeps are the point: the existing heartbeat thread
   uses `stop.wait(HEARTBEAT_S)` — copy the `stop.wait(...)` shape so shutdown is prompt, do not
   `time.sleep(1)` unconditionally in a way that outlives the reap.
3. In the existing `finally`, set the watchdog stop event alongside the heartbeat one, then
   `join(timeout=WATCHDOG_GRACE_S)`.
4. Distinguish the two exits in the result: if the child was killed by the watchdog set
   `crashed = True` and make `err` start with `f"wall-clock timeout after {HARD_TIMEOUT_S}s"` so the
   operator can tell a timeout from a crash. Keep the existing in-loop deadline check (it still
   handles a child that streams forever) but give it the same message prefix.
5. Do not change `HARD_TIMEOUT_S`'s value in this card.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, os, pathlib, tempfile, time, textwrap
sys.path.insert(0,'.')
fake = pathlib.Path(tempfile.mkdtemp())
(fake/"pi").write_text(textwrap.dedent('''
    #!/usr/bin/env python3
    import time
    time.sleep(999)      # silent, produces nothing on either stream
'''))
(fake/"pi").chmod(0o755); os.environ["PATH"] = f"{fake}:" + os.environ["PATH"]
import external.pi_cli as P
P.HARD_TIMEOUT_S = 2                       # monkeypatched clock, real value untouched
wd = pathlib.Path(tempfile.mkdtemp()); out = wd/"s.out"
t = time.monotonic(); r = P.run_pi_session(model="m", workdir=wd, prompt="p", out_file=out,
                                           log=lambda *a: None)
el = time.monotonic() - t
assert el < 20, f"watchdog did not fire (waited {el:.0f}s) — the silent child still hangs"
assert r.crashed is True, "timeout must be reported as a crash"
assert "wall-clock timeout" in r.err, r.err[:200]
print(f"watchdog ok ({el:.1f}s)")
PY
```
Must pass, plus the Gate.

## Out of scope
The stderr drain and the `stderr` field (T17, already landed), the verdict regex and case handling
(T19), `session.py`'s crash→verdict mapping and the new `no_verdict` value (T20), retry policy in
`Pipeline._run` (T41), making `HARD_TIMEOUT_S` configurable, and any change to `supervisor.py`'s
own `kill_tree`/`ChildTracker` (that kill path is correct and already keeps `start_new_session`).

## Done when
A silent child returns within `HARD_TIMEOUT_S + WATCHDOG_GRACE_S` seconds; the timeout is reported
as `crashed=True` with `wall-clock timeout` in `err`; the watchdog thread is a daemon and is joined
in the `finally`, so no thread survives the call.
