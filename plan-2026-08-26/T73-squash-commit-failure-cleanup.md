# T73 — Clean state when the squash commit command fails

**Depends:** T72 · **Leaf ticket**

## Context
This is one recursively-sliced behavior with one fixture class.

## Read first
- external/git_cli.py

## Do
Create the new file: `tests/test_git_commit_failure.py`.

Route commit failure through abort_merge; include stderr tail and starting trunk sha; if cleanup remains dirty raise and preserve evidence. Test with a controlled failing commit command.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_git_commit_failure -v
```
Global Gate must pass.

## Out of scope
No merge-conflict implementation, gate behavior, revert policy, or branch deletion.

## Done when
The named behavior and failure path are proven by the dedicated test and no out-of-scope file changed.
