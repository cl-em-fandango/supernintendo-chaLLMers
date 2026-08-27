# T21 — Write `status` into `task.json` on every terminal move

**Wave 5** · depends: none (after T01) · finding: F4

## Context
`TaskState.status` is written once by `intake()` and never rewritten: `park()`, `fail()` and
`complete()` in `harness/workflow/task_lifecycle.py` do a `shutil.move` of the task directory plus an
`_exec_summary()` write and leave the JSON alone. Live evidence:
`/home/donald/work/queue/parked/001-interrupt-handling/task.json` still says `"status": "active"`.
Directory location is the only truth and `TaskStatus` is decorative, so anything that reads
`task.json` (a future handover, an operator, `resume`'s reason line) is told the task is still
running. Fix the writers, not the readers.

## Read first
- `harness/workflow/task_lifecycle.py` — `TaskState` + `to_json`, `load_state`, `save_state`,
  `intake`, `park`, `fail`, `complete`, `write_atomic`, `task_json_path`
- `harness/core/enums.py` — `TaskStatus` (PENDING / ACTIVE / DONE / PARKED / FAILED)
- `harness/workflow/continue_fresh.py` — `in_flight_task_dirs()` treats "has a `task.json`" as
  in-flight; it must not start relying on `status` in this card

## Do
1. In each of `park`, `fail`, `complete`, **after** the `shutil.move` succeeds and **before**
   `_exec_summary`, load the state, set `status` to `TaskStatus.PARKED.value` / `FAILED.value` /
   `DONE.value` respectively, and `save_state` at the *new* path. Getting the order wrong writes a
   correct file into a directory that no longer exists.
2. Tolerate a missing or corrupt `task.json`: `load_state` is already tolerant — if it returns
   nothing usable, construct a minimal `TaskState` with `id = <directory name>`, the correct
   `status`, `source = "unknown"`, `created = _now()` and write it. A terminal move must never
   raise because of bookkeeping.
3. Keep `last_updated` semantics (`_now()`, UTC iso, seconds) — `save_state` should already do it;
   if the minimal-`TaskState` path bypasses it, set it explicitly.
4. Do **not** change any reader. `cmd_status` counts directories; `in_flight_task_dirs` looks for
   `task.json`; `resume_task` searches by directory. They stay as they are — this card only makes
   the JSON agree with the directory.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, tempfile, pathlib; sys.path.insert(0,'.')
from harness.core.config import Config
from harness.workflow.task_lifecycle import TaskLifecycle
root = pathlib.Path(tempfile.mkdtemp())
for d in ("pending","active","parked","failed","done","claimed","review"):
    (root/"queue"/d).mkdir(parents=True)
cfg = Config({"workDir": str(root)}, root/"queue")     # adapt to Config's real ctor signature
lc = TaskLifecycle(cfg, log=lambda *a: None)
tid = "t99-demo"
lc.intake(tid, "body", "test-source")                  # -> active/<tid>
src = cfg.queue_dir/"active"/tid
for verb, want in (("park","parked"), ("fail","failed"), ("complete","done")):
    d = cfg.queue_dir/"active"/tid                      # put the task back in active/ each round
    if not d.exists():
        d.mkdir(parents=True)
        lc.intake(tid, "body", "test-source")
    getattr(lc, verb)(tid)                              # all three are writers under test
    js = json.loads((cfg.queue_dir/want/tid/"task.json").read_text())
    assert js["status"] == want, (verb, js)
# a move with NO task.json must still leave a valid file, not raise
d = cfg.queue_dir/"active"/tid; d.mkdir(parents=True, exist_ok=True)
lc.park(tid)
js = json.loads((cfg.queue_dir/"parked"/tid/"task.json").read_text())
assert js["status"] == "parked" and js["id"] == tid and js.get("last_updated")
print("status on terminal moves ok")
PY
```
Must pass, plus the Gate. (Adapt the `Config` / lifecycle constructor calls to the real signatures
before running — the assertions are the contract, the setup is scaffolding.)

## Out of scope
Recording `workdir` (T22 — it touches `TaskState` too, so it goes *after* this card), per-slice
checkpointing (T26), changing `in_flight_task_dirs` or `cmd_status` to trust `status` (nobody owns
that yet; directory remains the authority), rewriting the two already-parked real tasks in
`/home/donald/work/queue` (T25 is read-only per decision D4), and any schema version field.

## Done when
A task moved by `park`/`fail`/`complete` has `status` equal to its new directory at the new path;
moving a directory with no `task.json` produces a minimal valid one instead of raising; the live
`queue/parked/001-interrupt-handling/task.json` is untouched (that one is an operator call, D4).
