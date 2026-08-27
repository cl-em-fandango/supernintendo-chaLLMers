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
4. On any early `return` or unhandled exit, the claims **this invocation made** are returned to
   `pending/` — record the names as they are claimed and requeue those in a `finally`; log how many.
   Do **not** call `provider.requeue_all_claims()` here: the live `claimed/` holds 7 pre-existing
   tasks and decision **D4** says they stay where they are until the human review pass. `cmd_run`
   cleaning up "every claim in the directory" would silently drain that evidence. Blanket requeueing
   belongs to the explicit operator command in T12, never to a run path.
5. The autonomous hand-off at the end of `cmd_run` must stay reachable but inert in tests — patch the
   generator name `cmd_run` uses with a no-op that records the call, so the verify block does not
   depend on `AutonomousGenerator`'s internals.
6. `cmd_run_one`: after processing, requeue the extras via T09's API (it already does this with
   the deleted shim) — verify, don't rewrite.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, pathlib, tempfile, types
sys.path.insert(0,'.')
import harness.cli.handlers as H
from harness.core.providers import DirectoryTaskProvider
q = pathlib.Path(tempfile.mkdtemp()); (q/"pending").mkdir(); (q/"claimed").mkdir()
for n in ("001-a","002-b","003-c"): (q/"pending"/f"{n}.md").write_text(f"# {n}\nwork on {n}\n")
(q/"claimed"/"099-preexisting.md").write_text("D4: not this run's claim\n")   # must survive
seen, gen = [], []
class Boom:                      # pipeline stub: raises on the first task, then must be skipped
    lifecycle = None
    def process(self, task):
        seen.append(task.id)
        if len(seen) == 1: raise RuntimeError("simulated park")
class NoGen:                     # autonomous hand-off made inert (see Do 5)
    def __init__(self, *a, **k): pass
    def run(self, *a, **k): gen.append("reached"); return 0
class Sink:
    def __call__(self, line=""): pass
    def close(self): pass
prov = DirectoryTaskProvider(q/"pending", q/"claimed")
# 6-tuple: build() gained the log sink in T07 — count the unpack in handlers.py and match it
H.build = lambda *a, **k: (types.SimpleNamespace(queue_dir=q, logs_dir=q/"logs"),
                           None, None, prov, Boom(), Sink())
if hasattr(H, "AutonomousGenerator"): H.AutonomousGenerator = NoGen
assert H.cmd_run() == 0
assert len(seen) == 3, f"not every pending task was attempted: {seen}"
left = {p.name for p in (q/"claimed").glob("*.md")}
assert left == {"099-preexisting.md"}, (
    f"this run left its own claims in claimed/, or it swept a claim that was not its own: {left}")
print("cmd_run no-leak ok", seen)
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate. Two notes: assert the **count and order** of processed tasks, never the
spelling of a task id (`001-a` → `001_a` or `001-a` is provider behaviour, not this card's); and the
one-file-still-in-`claimed/` assertion is the D4 guard — pre-existing claims are not this run's to
requeue.

## Out of scope
Supervisor cycle logic (T13/T14), stale-claim age policy (T12), autonomous generator internals,
`status` (T11), real queue files.

## Done when
Repro prints `cmd_run no-leak ok` with all three tasks processed despite task 1 raising; `claimed/`
holds nothing but the file this run did not claim; `grep -n "fetch_pending(claim=True)"
harness/cli/handlers.py` shows no unbounded claim left in `cmd_run`; no run path calls
`requeue_all_claims()` (that is T12's operator command only).
