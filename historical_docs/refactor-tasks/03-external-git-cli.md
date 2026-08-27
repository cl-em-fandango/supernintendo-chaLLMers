# Refactor Chunk 3: Isolate git subprocess in external/git_cli.py

## Context
CODING_STANDARDS.md §4: all subprocess calls live in `external/`. Today
`harness/gitops.py` calls `subprocess` for git directly. This chunk moves the
raw git mechanics into `external/git_cli.py` and makes `harness/gitops.py` a
thin wrapper (or folds its public API onto the external layer).

## Read first
- `CODING_STANDARDS.md` — §4
- `harness/gitops.py` — the whole file

## The split

**`external/git_cli.py`** owns the raw git mechanics (stdlib only, no harness
imports):
- `_git(cwd, *args, check=True) -> str` and `_has(cwd, ref) -> bool` (the
  subprocess wrappers)
- `LAST_GOOD_TAG = "pi/last-good"` constant
- `ensure_branch(workdir, task_id, trunk) -> str`
- `merge_to_trunk(workdir, task_id, trunk, title) -> None`
- `verify_harness(workdir) -> tuple[bool, str]`
- `_revert_to_last_good(workdir, trunk) -> None`

Move these functions verbatim into `external/git_cli.py`. They already only use
`subprocess`, `sys`, `pathlib` — no harness imports — so this is a clean move.

**`harness/gitops.py`** becomes a thin re-export so existing importers
(`harness/pipeline.py` does `from .gitops import ensure_branch, merge_to_trunk`)
keep working without change in this chunk:
```python
"""Git operations. Thin wrapper over external/git_cli (see CODING_STANDARDS §4)."""
from external.git_cli import (  # noqa: F401
    LAST_GOOD_TAG,
    ensure_branch,
    merge_to_trunk,
    verify_harness,
)
```
(If you prefer, you may instead update `pipeline.py` to import from
`external.git_cli` directly and delete `gitops.py` — but the re-export is the
smaller, safer change for this chunk. Note the re-export approach for a later
cleanup.)

## Rules
- `external/git_cli.py` imports stdlib only.
- `harness/gitops.py` no longer contains `subprocess`.
- `verify_harness` still runs `import harness` + `harness.py status` (unchanged).
- Behavior identical: same merge, same gate, same revert.

## Verify (the gate)
```
cd /home/donald/work/harness
python3 -c "import sys; sys.path.insert(0,'.'); import external.git_cli; print('external.git_cli ok')"
! grep -q "subprocess" harness/gitops.py && echo "gitops.py: no subprocess ✓"
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.gitops, external.git_cli; print('import ok')"
python3 harness.py status
```
All must pass.

## Commit
```
git add -A
git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "harness: isolate git subprocess in external/git_cli.py"
```
Then: `git tag -f pi/last-good pi/trunk`

## Done when
- `external/git_cli.py` exists, imports stdlib only, has all git functions
- `harness/gitops.py` has no `subprocess` (thin re-export)
- Gate passes (import + status)
- Committed and `pi/last-good` advanced
