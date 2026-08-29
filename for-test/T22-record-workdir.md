# T22 — Record the resolved `workdir` at intake and stop re-deriving it

**Wave 5** · depends: T21 · finding: F7

## Context
`TaskState` has no `workdir` field. Every run re-derives it with
`TaskLifecycle.resolve_workdir(task_dir)`, which regex-scrapes `/[a-zA-Z0-9_./-]+` out of
`original.md` and returns the first hit that is a directory containing `.git` — and, if nothing
matches, **the task directory itself**. So a task whose body does not name a repo ends up with the
queue directory as its workdir, which is exactly how `queue/active/002-pipeline-checkpoint-and-resume`
acquired a scratch `.git` with a single "harness: initial commit". Re-derivation is also
non-deterministic across runs: the regex takes the first path-looking token in prose.

## Read first
- `harness/workflow/task_lifecycle.py` — `TaskState` + `to_json`, `load_state`, `intake`,
  `resolve_workdir` (the last ~14 lines of the file)
- `harness/workflow/pipeline.py` — `process()`: where `resolve_workdir` is called and where the
  result feeds `ensure_branch` and the per-session workdir
- `harness/workflow/continue_fresh.py` — `task_from_dir` / `resume_in_flight`: the resume path is the
  one that must *not* re-derive

## Do
1. Add `workdir: str = ""` to `TaskState` and to `to_json`. Keep it last in the dataclass so
   positional construction elsewhere keeps working.
2. `load_state` must default a missing `workdir` to `""` — the old-format `task.json` files (the live
   `002` is one) have to keep loading, that is an existing tested behaviour.
3. Remove the intake/resolution circularity explicitly. A fresh task is intaken first so
   `original.md` and `task.json` exist; immediately afterwards call `resolve_workdir(task_dir)`, set
   `state.workdir`, and save it **before** `ensure_branch` or any session starts. Do not add a
   pre-resolved workdir argument to `intake()`.
4. On resume, use `state.workdir` directly. For an old-format state whose value is empty, call
   `resolve_workdir(task_dir)` once, log `workdir not recorded for <id>, re-derived from original.md`,
   persist the result, then continue. Thus both fresh intake and legacy migration use the same
   persisted `original.md` input and every later run is deterministic.
5. Do not delete `resolve_workdir` — T23 guards its result. It is called only immediately after fresh
   intake or once while migrating an old-format task, never on every resume.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, tempfile, pathlib; sys.path.insert(0,'.')
from harness.workflow.task_lifecycle import TaskState, TaskLifecycle
s = TaskState(id="x", status="active", source="t", created="now")
assert s.workdir == "" and json.loads(s.to_json() if isinstance(s.to_json(), str)
                                      else json.dumps(s.to_json()))["workdir"] == ""
s2 = TaskState(id="x", status="active", source="t", created="now", workdir="/tmp/repo")
d = json.loads(s2.to_json()) if isinstance(s2.to_json(), str) else s2.to_json()
assert d["workdir"] == "/tmp/repo"
# old-format task.json must still load, with workdir defaulted
root = pathlib.Path(tempfile.mkdtemp()); tj = root/"task.json"
tj.write_text(json.dumps({"id":"002","status":"active","source":"s","created":"2026-01-01T00:00:00",
                          "stage":"spec","history":[],"checkpointed_stages":None}))
lc = TaskLifecycle.__new__(TaskLifecycle)          # no cfg needed for a pure load
st = TaskLifecycle.load_state(lc, tj) if TaskLifecycle.load_state.__qualname__.count('.')==1 else None
assert st is None or st.workdir == ""
src = pathlib.Path('harness/workflow/pipeline.py').read_text()
assert 'state.workdir' in src and 'resolve_workdir' in src
# Permanent behavior test added by this card proves a fresh intake saves workdir before ensure_branch,
# and a second process() call does not invoke resolve_workdir again.
assert pathlib.Path('tests/test_workdir_persistence.py').exists()
print("workdir record shape ok")
PY
```
Must pass, plus the Gate.

## Out of scope
Refusing to `git init` inside the queue (T23 — it depends on this card so the guard has a recorded
workdir to test), the verification gate (T24), cleaning the live scratch `.git` out of
`queue/active/002…` (operator, deferred by D4), making `resolve_workdir` cleverer (it must get
*dumber*, not cleverer: it is being demoted, not improved), and per-slice checkpoints (T26).

## Done when
`task.json` contains `workdir` before git/session work starts; `tests/test_workdir_persistence.py`
proves a fresh intake resolves once and a resume reuses the saved value; an old-format `task.json`
loads with `workdir == ""`, is migrated once with a warning, and is deterministic thereafter.
