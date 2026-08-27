# Refactor Chunk 7: End-to-end verification (no code change)

## Context
Chunks 1–6 proved the harness still IMPORTS and that `status`/`report` run.
But imports passing does not prove a real session runs, verdicts parse, stats
record, and git branches work. This chunk is the proof before any automation is
allowed to run. It changes NO code — it exercises the runtime.

## Read first
- `REFACTOR_PLAN.md` — "Chunk 7"
- `harness/workflow/pipeline.py` — to know what a full run does

## Do

### 7a. Smoke session (proves external/pi_cli + session + stats work)
Run one real pi session through the refactored path and confirm it returns a
result with a verdict and records a stat:
```
cd /home/donald/work/harness
python3 -c "
import sys; sys.path.insert(0,'.')
from pathlib import Path
from harness.core.config import load
from harness.core.stats import StatsStore
from harness.core.session import SessionRunner
cfg = load('config.json')
store = StatsStore(cfg.stats_path)
runner = SessionRunner(cfg, store, log=print)
r = runner.run(cfg.model, '.', 'Reply with exactly: PONG', task_id='smoke', stage='smoke')
print('ok=', r.ok, 'verdict=', r.verdict, 'peak=', r.peak_tokens, 'crashed=', r.crashed)
assert r.ok, 'smoke session failed'
print('SMOKE OK')
"
```
Expect `ok=True`, `crashed=False`, a non-zero `peak_tokens`. (verdict may be
`unknown` — the model just says PONG; that's fine, we're testing the plumbing.)

### 7b. Git path (proves external/git_cli works)
Confirm the git helpers still work against a throwaway repo:
```
cd /tmp && rm -rf git-smoke && mkdir git-smoke && cd git-smoke
git init -q -b pi/trunk && git -c user.email=t@t -c user.name=t commit -qm init --allow-empty
python3 -c "
import sys; sys.path.insert(0,'/home/donald/work/harness')
from pathlib import Path
from external.git_cli import ensure_branch, verify_harness
wd = Path('/tmp/git-smoke')
branch = ensure_branch(wd, 'smoke-task', 'pi/trunk')
print('branch:', branch)
ok, detail = verify_harness(Path('/home/donald/work/harness'))
print('verify_harness on real harness:', ok, detail)
assert ok
print('GIT OK')
"
cd /tmp && rm -rf git-smoke
```

### 7c. Full task dry-run (proves the pipeline orchestrates)
Pick the smallest pending task and run it ONE stage deep to confirm intake,
branch creation, and the first stage all work through the new workflow/ layer.
Do NOT let it run to completion (that's what automation is for) — just confirm
it gets past intake into the first stage without an import/attribute error:
```
cd /home/donald/work/harness
# list pending to pick the smallest
ls -S /home/donald/work/queue/pending/ | tail -3
```
Then, using the smallest one, run `harness.py run-one` but be ready to stop it
after the first stage's `▶`/`◀` lines appear in the log. Confirm:
- a task dir appears in `queue/active/`
- `task.json` is written with `status: active`
- a git branch `pi/<task-id>` is created
- the first stage session starts (heartbeat visible)

If it runs to completion or parks, that's also acceptable — the point is it
runs through the refactored code without crashing on imports/attributes.
Afterward, clean up: if it parked/failed, move it back to pending so the queue
is intact for real automation:
```
# if the task ended up parked/failed, requeue it
python3 harness.py unpark <task-id>   # or manually move the dir back to pending/
```

## Rules
- NO code changes in this chunk. If something fails, STOP and report — do not
  fix inline. A failure here means an earlier chunk has a latent bug that must
  be fixed in its own chunk before proceeding.

## Verify
All three of 7a, 7b, 7c produce their `OK` / expected output with no
ImportError, AttributeError, or traceback.

## Commit
No code change, so no commit is required. If 7c left the queue in a non-pending
state, restore it (unpark) so the queue is clean.

## Done when
- 7a smoke session: `ok=True`, `crashed=False`, stat recorded
- 7b git path: branch created, `verify_harness` returns ok
- 7c dry-run: task gets past intake into the first stage with no traceback
- Queue restored to a clean pending state
- **This is the green light to start automation (the supervisor).**
