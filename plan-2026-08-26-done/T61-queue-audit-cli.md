# T61 — Wire queue-audit output and report persistence

**Depends:** T77 · **Leaf ticket**

> **BLOCKED — MOVED UNACTIONED (dependency not landed).** This leaf dispatches over a report that is
> not in the tree: `harness/workflow/queue_audit.py` (`audit_queue(cfg)`, `render_audit(cfg)`) is
> created by T76 and extended by T77, and both leaves are still open in `plan-2026-08-26/`. Verified
> by grep at move time: no `queue_audit` module, no `audit_queue`/`render_audit` definition, no
> `cmd_queue_audit`, and none of `tests/test_queue_audit_inventory.py`,
> `tests/test_queue_audit_artifacts.py`. Its own *Out of scope* forbids the anomaly logic that would
> have to be written to make a dispatch meaningful, so actioning this card now would be T76 + T77 +
> T61 in one session. **Enqueue T76, then T77, then re-enqueue this file unchanged** — the requirement
> below is intact and becomes actionable the moment T77 lands.

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below. Its suite is named for
the behavior (`tests/test_queue_audit_cli.py`) so no sibling leaf shares a file with it — T52 and
this card both claimed `tests/test_handlers.py`, which no longer reverts independently.

## Read first
- harness/cli/parser.py
- harness/cli/handlers.py

## Do
Create the new file: `tests/test_queue_audit_cli.py`.

Add queue-audit dispatch; print core report and write one new dated report under logs. Test with temp workDir and unchanged temp queue hash.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_queue_audit_cli -v
```
Global Gate must pass.

## Out of scope
No anomaly logic, queue writes, or operator action execution.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
