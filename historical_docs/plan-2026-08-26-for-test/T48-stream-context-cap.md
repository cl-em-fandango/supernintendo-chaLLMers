# T48 — Terminate pi from the streamed over-cap event

**Depends:** T17, T18, T32 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below. Its suite is named for
the behavior (`tests/test_pi_over_cap_stream.py`) so no sibling leaf shares a file with it — T35 owns
`tests/test_pi_subprocess.py`, and two leaves writing one module cannot be reverted apart.

## Read first
- external/pi_cli.py

## Do
Create the new file: `tests/test_pi_over_cap_stream.py`.

Add `max_context_tokens`; terminate/reap on the first streamed usage value strictly over the cap; return structured peak/limit/over-cap fields. Add fake-pi tests for 60000, 60001, prompt termination, and no deadlock.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_pi_over_cap_stream -v
```
Global Gate must pass.

## Out of scope
No stats, pipeline routing, parking, or markdown.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
