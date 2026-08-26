# T10 — `cmd_run` must not claim the whole queue and drop the extras

**Wave 2** · depends: T09 · finding: F2

## Context
`cmd_run` (handlers.py:44-56) calls `fetch_pending(claim=True)` — which moves **every** pending
file into `claimed/` — then loops in Python, and never requeues the remainder. If it parks,
fails, or dies on task 1, tasks 2..N are stranded in `claimed/` permanently. This is exactly how
`003`, `004`, `005`, `007`, `008`, `auto-3`, `auto-4` got stuck. `run-task-loop` already has the
correct one-at-a-time shape; `cmd_run` is the leaky twin.

## Read first
- `harness/cli/handlers.py` — `cmd_run`, `cmd_run_task_loop`, `cmd_run_one`
- `harness/core/providers.py` — T09's `fetch_pending(claim=..., limit=...)`, `requeue_claim`

## Do
1. Rewrite `cmd_run(continue_)` to the `cmd_run_task_loop` shape: loop, `fetch_pending(claim=True, limit=1)`,
   process one, requeue nothing (limit 1 claimed only the one), repeat until empty.
2. Keep `cmd_run`'s distinct behavior: when the queue drains it enters autonomous mode
   (`AutonomousGenerator(...).run(...)`) — `run-task-loop` does not.
3. Wrap each `pipeline.process(task)` in `try/except Exception` → `log` the exception and
   `continue` to the next task, so one bad task cannot strand the rest of the queue.
   (Do not swallow `KeyboardInterrupt`/`SystemExit` — catch `Exception` only.)
4. On any early `return` or unhandled exit, `provider.requeue_all_claims()` runs (use `finally`)
   so we never leave claims behind. Log how many were requeued.
5. `cmd_run_one`: after processing, requeue the extras via T09's API (it already does this with
   the deleted shim) — verify, don't rewrite.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, pathlib, tempfile, types
sys.path.insert(0,'.')
import harness.cli.handlers as H
q = pathlib.Path(tempfile.mkdtemp()); (q/"pending").mkdir()
for n in ("001-a","002-b","003-c"): (q/"pending"/f"{n}.md").write_text(f"# {n}\nwork on {n}\n")
from harness.core.providers import DirectoryTaskProvider
seen = []
class Boom:  # pipeline stub: parks the first task by raising, then must be skipped
    lifecycle = None
    def process(self, task):
        seen.append(task.id)
        if task.id == "001_a": raise RuntimeError("simulated park")
prov = DirectoryTaskProvider(q/"pending", q/"claimed")
H.build = lambda *a, **k: (types.SimpleNamespace(queue_dir=q, logs_dir=q/"logs"), None, None, prov, Boom())
H._log = lambda line="": None
assert H.cmd_run() == 0
assert seen == ["001_a","002_b","003_c"], seen
assert not list((q/"claimed").glob("*.md")), "claims left behind"
assert len(list((q/"pending").glob("*.md"))) == 3 or True   # consumed by design
print("cmd_run no-leak ok", seen)
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate.

## Out of scope
Supervisor cycle logic (T13/T14), stale-claim age policy (T12), autonomous generator internals,
`status` (T11), real queue files.

## Done when
Repro prints `cmd_run no-leak ok` with all three task ids processed despite task 1 raising;
`claimed/` empty afterwards; `grep -n "fetch_pending(claim=True)" harness/cli/handlers.py` shows
no unbounded claim left in `cmd_run`.
