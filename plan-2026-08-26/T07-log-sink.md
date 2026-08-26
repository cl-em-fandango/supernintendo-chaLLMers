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
3. `harness/cli/handlers.py`: delete its `_log` and use the sink from `build()` — add a module
   private `_set_log(sink)` / or return the sink from `build()` as a 6th tuple element. Choose
   the tuple change (explicit, no global) and update all `build()` unpackers
   (`grep -rn "build()" harness/ *.py`).
4. Do not change any log *text*, only where it goes.

## Verify
```bash
cd /home/donald/work/harness
rm -f /home/donald/work/logs/harness.log
python3 harness.py status >/dev/null; echo rc=$?
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
