# T76 — Read-only queue inventory and task-state anomalies

**Depends:** T21, T22, T23, T51 · **Leaf ticket** (first leaf of the re-sliced T60)

## Context
T60 grew past the hard limits in `RECURSIVE-SLICING-ALGORITHM.md`: its `Do` listed seven unrelated
anomaly classes in one ticket, over the five-criterion ceiling, and `fits()` Q8 applies — an agent
could implement the queue-git check and silently regress the status check. This leaf owns the
inventory plus the anomalies the task-dir walk already sees. The stray-output, duplicate-slug,
claim-metadata and short-body checks are T77.

## Read first
- harness/workflow/task_lifecycle.py
- harness/core/config.py
- harness/core/providers.py

## Do
Create the new files: `harness/workflow/queue_audit.py`, `tests/test_queue_audit_inventory.py`.

1. `audit_queue(cfg) -> list[str]` and `render_audit(cfg) -> str`, read-only: no `shutil`,
   `os.remove`, `open(..., "w")` or `Path.write_*` on any path under `cfg.queue_dir`.
2. Per-directory counts for `pending, active, claimed, done, parked, failed, review`, in that order.
3. One row per task dir: id, directory, `status` from `task.json`, `checkpointed_stages`, `workdir`
   (`—` when absent), and whether the dir contains a `.git`.
4. `ANOMALY` line when `status` disagrees with the directory holding the task dir.
5. `ANOMALY` line when `task.json` is missing or old-format (no `checkpointed_stages`, no
   `last_updated`).

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_queue_audit_inventory -v
```
Global Gate must pass.

## Out of scope
Stray `.pi-session-*.out` files, duplicate slugs across locations, claim ownership metadata, short
bodies and the `SUGGESTED OPERATOR ACTIONS` footer (T77); the CLI subcommand, stdout printing and
dated report file (T61); any mutation of a queue path (D4 postponed that to the human review pass).

## Done when
On a temp queue the five behaviors above appear in the report, the temp tree is byte-identical before
and after `render_audit`, the named test proves both, the Gate passes, and no out-of-scope file
changed.
