# T55 — Run both review fixes on the implementer

**Depends:** T29, T30 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_review_fix_model.py`.

Keep reviewer selection unchanged; use implementer for both technical and functional fix sessions. Test exact model calls.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_review_fix_model -v
```
Global Gate must pass.

## Out of scope
No prompt, stage, verdict, or progress-path changes.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
