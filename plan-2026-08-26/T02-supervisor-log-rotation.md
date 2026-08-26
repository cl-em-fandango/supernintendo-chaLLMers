# T02 — Bound `supervisor.log` (179 MB today, no rotation)

**Wave 0** · depends: T01 · `[tag]` · finding: F13

## Context
`/home/donald/work/logs/supervisor.log` is 179 MB / ~6 M lines, unbounded, and its tail is
a `[DRY] run-task-loop --continue` spin storm. `supervisor.py log()` appends forever. Disk
full = the supervisor and every pi session die at once. (The spin itself is T15; here we
only bound the log.)

## Read first
- `supervisor.py` — `log()`, and the `LOG`/`PIDFILE`/`STOPFILE` constants (~line 40)
- `harness/core/config.py` — `logs_dir`

## Do
1. In `supervisor.py`, rotate before appending: if `LOG.stat().st_size + len(line) > MAX_LOG_BYTES`
   (module constant, default `5_000_000`, env-overridable `SUPERVISOR_MAX_LOG_BYTES`),
   rename `LOG` -> `LOG.1` (replacing any existing `LOG.1`), then continue appending to a
   fresh `LOG`. Exactly one generation — no date-suffixed pile-up.
2. Rotation must never crash the supervisor: wrap in `try/(OSError, OSError-subclasses)` and,
   on failure, keep appending un-rotated and print a warning line once per process.
3. Add a `--help`-visible note in the module docstring naming the cap and the env var.
4. One-time ops: truncate the existing 179 MB file in place
   (`: > /home/donald/work/logs/supervisor.log` is fine — nothing holds it open; verify with
   `fuser` / `lsof` first, and if a process holds it, STOP and report instead of truncating).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import importlib, pathlib, sys, tempfile
sys.path.insert(0, '.')
import supervisor as S
d = pathlib.Path(tempfile.mkdtemp()); log = d / "supervisor.log"
S.LOG = log; S.MAX_LOG_BYTES = 200
for i in range(60): S.log(f"line {i} " + "x" * 20)
assert log.exists() and (d / "supervisor.log.1").exists(), "no rotation happened"
assert log.stat().st_size <= 400, f"current log too big: {log.stat().st_size}"
print("rotation ok")
PY
ls -l /home/donald/work/logs/supervisor.log     # small
```
Gate must pass.

## Out of scope
Rotating `harness.log` (T07 owns that file — do not touch its writer), the no-progress spin
(T15), log format changes, adding `logging` module machinery.

## Done when
The repro above prints `rotation ok`; at most one `.1` generation exists; the 179 MB file is
gone or < 1 MB; a rotation failure cannot kill the loop.
