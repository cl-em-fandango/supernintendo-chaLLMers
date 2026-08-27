# T08 — Stop sending supervised child output to `/dev/null`

**Wave 1** · depends: T07 · finding: F3 (supervisor half)

## Context
`ChildTracker.spawn` (`supervisor.py:113-119`) uses `stdout=DEVNULL, stderr=DEVNULL`. In
supervised mode every verdict line, `▶`/`◀` stage line, heartbeat, warning and **traceback**
disappears. When a task parks, there is no record of why. T07 made the child write
`harness.log` itself, which covers most of it — but a Python traceback printed *to stderr*
and anything written before a hard kill still vanish, and rc-only logging makes triage guesswork.

## Read first
- `supervisor.py` — `ChildTracker` (whole class), `run_loop()`'s two `tracker.spawn(...)` calls
- `harness/core/logsink.py` (from T07) for the rotation convention to mirror

## Do
1. `spawn(self, args: list[str], *, label: str) -> int`: create
   `<WORK_DIR>/logs/children/<UTC ts>-<label>.log` (`mkdir(parents=True, exist_ok=True)`),
   open it, and pass the handle as both `stdout=` and `stderr=` (same fd keeps their relative
   order). Keep `start_new_session=True` exactly as-is — the tree-kill depends on it.
2. Write two banner lines around the child's output in the same file:
   `=== spawn <label> args=<args> ===` and `=== exited rc=<rc> ===`, flushed before close.
3. Always close the file handle (`finally`).
4. Cap the children dir: before spawning, delete the oldest files so at most
   `MAX_CHILD_LOGS` (default 50, env `SUPERVISOR_MAX_CHILD_LOGS`) remain.
5. Pass `label` from the call sites: `"status"`, `"run-task-loop"` / `"run-one"` / `"autonomous"`
   (whatever subcommand the call site uses after T14).
6. `supervisor.py`'s own log line for each spawn must name the child log path, so a human can
   `tail` the right file.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, pathlib, tempfile
sys.path.insert(0,'.')
import supervisor as S
d = pathlib.Path(tempfile.mkdtemp()); S.WORK_DIR = d
t = S.ChildTracker()
rc = t.spawn([sys.executable, "-c", "print('child-out'); import sys; print('child-err', file=sys.stderr)"], label="probe")
files = sorted((d/"logs"/"children").glob("*probe*"))
assert rc == 0 and len(files) == 1, files
txt = files[0].read_text()
assert "child-out" in txt and "child-err" in txt and "exited rc=0" in txt, txt
print("child capture ok ->", files[0].name)
PY
```
Must pass, plus the Gate. Do not run the real supervisor loop.

## Out of scope
The cycle decision (T13/T14), harness-side log sink (T07), log levels, real-time tailing.

## Done when
Repro prints `child capture ok`; `DEVNULL` appears nowhere in `supervisor.py` for child
output; the children dir cannot grow past `MAX_CHILD_LOGS`.
