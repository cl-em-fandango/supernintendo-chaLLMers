# T56 — Separate review feedback from implementation progress

**Depends:** T30 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_review_note_path.py`.

Write review feedback to slice-<id>-review.md while preserving implementation progress path. Test both files survive.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_review_note_path -v
```
Global Gate must pass.

## Out of scope
No model selection, prompt content, or artifact migration.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
