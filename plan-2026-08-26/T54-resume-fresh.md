# T54 — Add `resume --fresh` only

**Depends:** T01 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/cli/parser.py
- harness/workflow/resume.py
- tests/test_resume_cli.py

## Do
Wire the flag to existing fresh_restart. Add one test that fresh drops checkpoints and one that unpark preserves them.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_resume_cli -v
```
Global Gate must pass.

## Out of scope
No README, pipeline, provider, or other resume changes.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
