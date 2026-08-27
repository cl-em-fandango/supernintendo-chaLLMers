# T68 — Test parser and dispatch reachability

**Depends:** T16, T54, T61 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/cli/parser.py
- harness/cli/handlers.py
- harness.py

## Do
Create the new file: `tests/test_cli_surface.py`.

Enumerate parser subcommands and assert each main dispatch target exists; assert no public cmd_* handler is unreachable.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_cli_surface -v
```
Global Gate must pass.

## Out of scope
No handler behavior, subprocess, queue, or documentation.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
