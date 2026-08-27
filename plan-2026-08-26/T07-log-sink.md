# T07 — One log sink that actually writes `work/logs/harness.log`

**Wave 1** · depends: T01 · `[tag]` · findings: F3 (in-process half), F9 (`_log` duplication)

## Context
`composition._log` and `handlers._log` are two identical `print(line, flush=True)` functions.
README claims `work/logs/harness.log` is written; nothing writes it — `cfg.logs_dir` is only
`mkdir`'d. So every verdict line, heartbeat and warning from `harness.py` is stdout-only and
lost when stdout is redirected or discarded (which the supervisor does — see T08).

## Read first
- `harness/composition.py` (whole, 28 lines) and `harness/cli/handlers.py` (top 20 lines)
- `harness/core/config.py` — `logs_dir`
- Call sites: `grep -rn "_log\b" harness/ | head -40`

## Do
1. New file `harness/core/logsink.py` (stdlib only; CODING_STANDARDS §1 one responsibility):
   - `class LogSink: def __init__(self, path: Path | None, echo: bool = True, max_bytes: int = 5_000_000)`
   - `__call__(self, line: str = "") -> None` — echo to stdout (if `echo`) and append to `path`
     with a `[ISO8601] ` prefix. Never raises: any `OSError` degrades to echo-only, warned once.
   - rotate at `max_bytes` to `<path>.1`, one generation (same shape as T02's supervisor rule).
   - `close()` for tests.
2. `composition.build()` constructs the sink (`cfg.logs_dir / "harness.log"`) and passes it as
   the `log=` argument everywhere it currently passes `_log` (runner, pipeline).
3. `harness/cli/handlers.py`: delete its `_log` and use the sink from `build()` — return the sink
   from `build()` as a **6th tuple element** (explicit, no global) and update all `build()`
   unpackers (`grep -rn "build()" harness/ *.py`). This changes `build()`'s return arity from 5 to 6:
   write **`build() now returns 6`** in the commit message, because the later cards that stub
   `build()` (T10, T11, T12 verify blocks and T37's `stub_build`) were written against the 5-tuple
   and each must extend its stub — a 5-element stub against a 6-way unpack is a `ValueError` in
   someone else's card.
4. Do not change any log *text*, only where it goes.

## Verify
Writing this file is the narrow `AGENTS.MD` exception recorded in `PLAN-2026-08-26.md` §Rules: the
append *is* the acceptance criterion. Delete nothing — if `harness.log` already exists, note its size
first and assert growth instead.

```bash
cd /home/donald/work/harness
before=$(stat -c %s /home/donald/work/logs/harness.log 2>/dev/null || echo 0)
python3 harness.py status >/dev/null; echo rc=$?
after=$(stat -c %s /home/donald/work/logs/harness.log)
test "$after" -gt "$before" && echo "harness.log grew ✓"
test -s /home/donald/work/logs/harness.log && echo "harness.log written ✓"
grep -c "^\[" /home/donald/work/logs/harness.log            # >= 1 timestamped line
! grep -rn "def _log" harness/ | grep -q . && echo "no duplicate _log ✓"
python3 - <<'PY'
import sys, pathlib, tempfile
sys.path.insert(0,'.')
from harness.core.logsink import LogSink
p = pathlib.Path(tempfile.mkdtemp())/"h.log"
s = LogSink(p, echo=False, max_bytes=120)
for i in range(40): s(f"line {i} paddedtext")
assert (p).exists() and (p.with_name("h.log.1")).exists(), "rotation missing"
s.close(); print("sink ok")
PY
```
All must pass, plus the Gate (unittest count unchanged).

## Out of scope
Supervisor child capture (T08), log levels/structured logging, changing message wording,
rotating `supervisor.log` (T02).

## Done when
`harness.log` exists and grows on any `harness.py` command; exactly one log-writing
implementation exists in `harness/`; a disk error cannot break a pipeline run.
