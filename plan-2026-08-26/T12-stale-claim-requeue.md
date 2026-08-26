# T12 — `harness.py requeue-claims` (+ stale claims reclaimed at loop start)

**Wave 2** · depends: T09, T11 · finding: F2

## Context
There is no command that recovers a stranded claim. `unpark` handles `parked/`+`failed/` only,
and `_requeue_claimed` (deleted in T09) was only reachable mid-run. This card ships the operator
command and the automatic guard so the wave-2 leak cannot silently recur.

## Read first
- `harness/cli/handlers.py` — `cmd_unpark` (the pattern + `_log` usage to follow)
- `harness/cli/parser.py` — whole file, 66 lines; note the hidden `requeue` parser at line 58
- `harness/core/providers.py` — T09's `list_claims`, `requeue_claim`, `requeue_all_claims`, `claim_age_hours`

## Do
1. `parser.py`: `requeue-claims` subcommand with `--older-than HOURS` (float, default `0.0` =
   everything) and `--dry-run`.
2. `harness.py`: dispatch it to `handlers.cmd_requeue_claims(older_than, dry_run)`.
3. `cmd_requeue_claims`: list claims; select those with `claim_age_hours(...) >= older_than`
   (treat `-1.0` as "skip"); dry-run prints the plan and returns 0; otherwise requeue each and
   print `requeued <name> (<age>h)` per line plus a final `requeued N of M`.
   Returns 0 always (an empty claim dir is not an error).
4. Automatic guard: at the **start** of `cmd_run_task_loop` (and `cmd_run`), before any
   `fetch_pending`, requeue claims older than `CLAIM_STALE_HOURS` (module constant, default `6.0`,
   env-overridable) and log `requeued N stale claim(s) (>= 6h)`. A fresh claim (< 6h) is left
   alone — that is a concurrent run's, not garbage.
5. `README.md`: add the command to the command list with one line of prose
   (full README rework is T33 — do not restructure the file).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, pathlib, tempfile, os, time
sys.path.insert(0,'.')
from harness.core.providers import DirectoryTaskProvider
import harness.cli.handlers as H
q = pathlib.Path(tempfile.mkdtemp())
for s in ("pending","claimed","active","review","done","failed","parked"): (q/s).mkdir()
old = q/"claimed"/"001-old.md"; old.write_text("o"); os.utime(old, (time.time()-7200,)*2)
new = q/"claimed"/"002-new.md"; new.write_text("n")
prov = DirectoryTaskProvider(q/"pending", q/"claimed")
H.build = lambda *a, **k: (type("C",(),{"queue_dir":q,"logs_dir":q/"logs"})(), None, None, prov, None)
assert H.cmd_requeue_claims(older_than=6.0, dry_run=True) == 0
assert len(list((q/"claimed").glob("*.md"))) == 2, "dry run moved files"
assert H.cmd_requeue_claims(older_than=6.0, dry_run=False) == 0
names = {p.name for p in (q/"claimed").glob("*.md")}
assert names == {"002-new.md"}, names
assert (q/"pending"/"001-old.md").exists()
print("stale requeue ok")
PY
python3 harness.py requeue-claims --dry-run ; echo rc=$?    # rc=0, prints a plan, moves nothing
ls /home/donald/work/queue/claimed/ | wc -l                  # UNCHANGED by a --dry-run
```
All must pass, plus the Gate.

## Out of scope
Requeueing the real queue without `--dry-run` (that is T25, with the human), changing `unpark`,
supervisor cycle logic (T13/T14), file locking/`flock`.

## Done when
`requeue-claims --dry-run` is safe and idempotent; only claims older than the threshold move;
both long-running entrypoints self-heal stale claims at start; `--dry-run` left the real
`claimed/` count identical.
