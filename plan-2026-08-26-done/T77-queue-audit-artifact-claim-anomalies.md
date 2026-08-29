# T77 — Queue artifact, duplicate-slug and claim anomalies plus the operator footer

**Depends:** T76 · **Leaf ticket** (second leaf of the re-sliced T60)

> **BLOCKED — MOVED UNACTIONED (dependency not landed).** Every check in this leaf is an addition to a
> report that is not in the tree: `harness/workflow/queue_audit.py` (`audit_queue(cfg)`,
> `render_audit(cfg)`) is created by T76, and T76 is still open in `plan-2026-08-26/`. Verified by
> grep at move time: no `queue_audit` module, no `audit_queue`/`render_audit` definition, and no
> `tests/test_queue_audit_inventory.py`. Its own *Out of scope* gives the directory counts, task rows,
> status/state anomalies and `.git` detection to T76, so actioning this card now would mean writing
> T76's module here too — two features in one session, and a `tests/test_queue_audit_artifacts.py`
> that cannot assert anything about a module it would have to author. **Enqueue T76 first, then
> re-enqueue this file unchanged** — the requirement below is intact and becomes actionable the moment
> T76 lands.

## Context
The second half of the split described in T76: the anomalies that need something other than the
task-dir walk — stray session outputs, the same slug in two lifecycle locations, claim ownership
metadata and body length — plus the footer that turns findings into hand-run operator commands.

## Read first
- harness/workflow/queue_audit.py
- harness/core/config.py
- harness/core/providers.py

## Do
Create the new file: `tests/test_queue_audit_artifacts.py`.

Add five `ANOMALY` checks to `audit_queue` and one footer to `render_audit`, still read-only:

1. `.pi-session-*.out` under `cfg.queue_dir` or under `cfg.logs_dir`.
2. The same slug present in more than one lifecycle location.
3. A claim whose ownership metadata (T51) is missing or corrupt.
4. A task body shorter than 200 characters.
5. A trailing `SUGGESTED OPERATOR ACTIONS` section pairing each finding with the command an operator
   would run by hand; the harness never runs it.

A claimed-only file is normal claim state and must not be reported merely because no pending copy
exists.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_queue_audit_artifacts -v
```
Global Gate must pass.

## Out of scope
Directory counts, task rows, status/state anomalies and `.git` detection (T76); the CLI subcommand
and dated report file (T61); requeueing, deleting outputs, removing a scratch `.git` or editing any
task body (D4 postponed all mutation to the human review pass).

## Done when
On a temp queue each of the five checks fires once for its fixture and the footer lists the suggested
commands, the temp tree is byte-identical before and after, the named test proves all of it, the Gate
passes, and no out-of-scope file changed.
