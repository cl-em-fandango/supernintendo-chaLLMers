# T02 — Bound `supervisor.log` (186 MB today, no rotation)

**Wave 0** · depends: T01 · `[tag]` · finding: F13

## Context
`/home/donald/work/logs/supervisor.log` is 186 MB / ~6 M lines (audit §F13), unbounded, and its tail is
a `[DRY] run-task-loop --continue` spin storm. `supervisor.py log()` appends forever. Disk
full = the supervisor and every pi session die at once. (The spin itself is T15; here we
only bound the log.)

## Read first
- `supervisor.py` — `log()`, and the `LOG`/`PIDFILE`/`STOPFILE` constants (~line 40)
- `harness/core/config.py` — `logs_dir`

## Do
1. In `supervisor.py`, format the complete record first (prefix, line, newline), encode it with the
   file's UTF-8 encoding, and rotate before appending when
   `LOG.stat().st_size + len(encoded_record) > MAX_LOG_BYTES` (module constant, default `5_000_000`,
   env-overridable `SUPERVISOR_MAX_LOG_BYTES`),
   rename `LOG` -> `LOG.1` (replacing any existing `LOG.1`), then continue appending to a
   fresh `LOG`. Exactly one generation — no date-suffixed pile-up.
2. Rotation must never crash the supervisor: wrap in `try/(OSError, OSError-subclasses)` and,
   on failure, keep appending un-rotated and print a warning line once per process.
3. Add a `--help`-visible note in the module docstring naming the cap and the env var.
4. One-time ops: the existing 186 MB file needs truncating in place. **`AGENTS.MD` forbids writing to
   `/home/donald/work/logs`, and a truncation is not an append — so this is an operator step, not a
   card step.** Print the exact command and the pre-check in the commit message and hand it to the
   human:
   `lsof /home/donald/work/logs/supervisor.log` (must be empty), then
   `: > /home/donald/work/logs/supervisor.log`.
   Do not run it. If the human is not present, the card still completes: the rotation code is the
   deliverable, the truncation is a logged follow-up.

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
# the rotation code is proven by the repro above; the real file is the human's step (see Do 4)
ls -l /home/donald/work/logs/supervisor.log
```
Gate must pass.

## Out of scope
Rotating `harness.log` (T07 owns that file — do not touch its writer), the no-progress spin
(T15), log format changes, adding `logging` module machinery.

## Done when
The repro above prints `rotation ok`; at most one `.1` generation exists; a rotation failure cannot
kill the loop; the truncation command is recorded in the commit message as a human step (it is not
run by this card).
