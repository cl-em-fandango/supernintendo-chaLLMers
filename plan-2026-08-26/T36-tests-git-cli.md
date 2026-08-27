# T36 — Temp-repo tests for `git_cli`: merge, gate, revert, abort, dirty refusal

**Wave 9** · depends: T03, T04, T05, T23, T24, T27 · finding: F11

## Context
`external/git_cli.py` is where the destructive commands live — `merge --squash`, `reset --hard`,
`branch -d`, `tag -f` — and it has zero tests. The revert path was found broken only by inspection
(F6a: `_has()` probed `refs/heads/`), and wave 0 fixed four separate bugs in it (tag lookup, merge
abort, dirty refusal, breaker routing). None of those fixes has a regression test, so the next edit
to this file can silently un-fix them. All of it is testable with throwaway repos and no network.

## Read first
- `external/git_cli.py` — `_git`, `has_branch` / `has_tag`, `merge_in_progress` (T04),
  `ensure_branch`, `merge_to_trunk`, `verify_harness`, `_revert_to_last_good`, `is_under_queue`
  (T23), `gate_applies` / `GateNotApplicable` (T24), `cleanup_branch` (T27).
  **`_has` is gone** — T03 deletes it and replaces it with the two explicit predicates, so this file
  must neither call it nor name it (the audit's `_has` is history, not API).
- `plan-2026-08-26/T03-git-tag-lookup.md`, `T04-merge-abort.md`, `T05-dirty-tree-guard.md`,
  `T24-refuse-merge-without-gate.md`, `T27-merge-checkpoint.md` — each names the bug to pin
- `tests/test_checkpoint_state.py` — house style

## Do
1. New file `tests/test_git_cli.py`. Helper `def make_repo(tmp, *, with_harness=False) -> Path`:
   `git init -q -b trunk`, optional `with_harness` writing `harness.py` + `harness/__init__.py` +
   `harness/composition.py` so `gate_applies()` can be true without a real harness (**T24 refuses the
   merge before any git write unless `harness.py` *and* `harness/composition.py` both exist** — every
   merge case needs them; `__init__.py` is for anything that imports the package), one commit, author set with `-c user.email` and `-c user.name` on every commit (never rely
   on global git config).
2. Cases:
   **a.** tag vs branch lookup in T03's API — the exact F6a inversion: in a repo whose `pi/last-good`
   exists **only as a tag**, `has_tag(cwd, "pi/last-good")` is True and `has_branch(cwd,
   "pi/last-good")` is False; neither predicate reports a ref that was never created
   (`has_branch(cwd, "pi/nope")` / `has_tag(cwd, "pi/nope")` are False); and `_revert_to_last_good`
   on that repo returns a `tag:`-prefixed string, not `"HEAD~1"` — the broken `_has()` probed
   `refs/heads/` and always fell back.
   **b.** `ensure_branch` creates `pi/<id>` from trunk and is idempotent when called twice.
   **c.** `ensure_branch` + `is_under_queue`: a queue path is refused by the workflow guard (mirror
   T23's assertions here so the pair stays tested).
   **d.** happy merge: the scratch repo must satisfy `gate_applies` (see Do 1) or T24's
   `GateNotApplicable` fires before the merge and the case proves nothing. Satisfy the *recognition*
   check with the stub files and then monkeypatch `verify_harness` to `True` for the run (exactly what
   T27's verify block does) — read `verify_harness` first, and do **not** weaken the real gate or
   make the stub repo genuinely importable just to please it. Then: trunk moves, tag `pi/last-good`
   updated, and the branch still exists until `cleanup_branch` (T27).
   **e.** gate fails → trunk is back at the pre-merge commit, `pi/last-good` unchanged, exception
   raised, and the repo is not left mid-merge. Assert that as **`git ls-files -u` empty + `merge_in_progress(workdir)`
   False + `git status --porcelain` empty**. Do **not** assert on `.git/MERGE_HEAD`: `merge --squash`
   never writes one, so "no MERGE_HEAD left behind" is an assertion that cannot fail (T04).
   **f.** conflicting merge → exception raised, worktree clean — same three assertions as (e), plus
   the file the branch added is gone and no `<<<<<<<` markers remain in the worktree. T04's own repro
   is the model; `MERGE_HEAD` is not a valid oracle here either.
   **g.** dirty worktree → `_revert_to_last_good` refuses rather than destroying the dirty file
   (T05): assert the dirty file's contents are **unchanged**.
   **h.** `GateNotApplicable` → raised before any git write, repo HEAD unchanged (T24).
3. Every repo lives under `tempfile.mkdtemp()`; cleanup via `addCleanup`. No test may touch
   `/home/donald/work/harness` or `/home/donald/work/queue` — assert the repo path starts with the
   temp root inside each case.
4. Never call `git reset --hard` against a repo the test did not create; never touch a real ref.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, unittest, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_git_cli.py'); assert p.exists()
src = p.read_text()
assert '/home/donald/work/queue' not in src, "test references the real queue"
assert 'has_tag' in src and 'has_branch' in src, "case (a) missing, or written against the deleted _has"
assert 'merge_in_progress' in src, "abort cases never assert on T04's predicate"
assert 'ls-files' in src and 'porcelain' in src, \
    "abort cases must assert on git ls-files -u / --porcelain, not MERGE_HEAD"
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_git_cli')
assert suite.countTestCases() >= 8, f"only {suite.countTestCases()} cases (a-h)"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"git_cli tests ok ({suite.countTestCases()} cases)")
PY
```
Must pass, plus the Gate.

## Out of scope
The supervisor's breaker that *calls* git (T38), the verification gate's per-project design (deferred
by **D3** — do not add a `verifyCommands` config key to make testing easier), `pipeline`'s
checkpoint bookkeeping around a merge (T27 owns that test itself now — its Do 6 ships
`tests/test_merge_checkpoint.py`; no wave-9 card covers `workflow/pipeline`), real remotes or pushes
(there is no remote — D5), and any test that runs against this repo's real `.git`.

## Done when
`tests/test_git_cli.py` has ≥8 green cases (a–h) covering tag-vs-branch lookup through
`has_tag`/`has_branch`, merge, gate-fail revert, conflict abort (asserted with `git ls-files -u`,
`merge_in_progress` and a clean `--porcelain` — never `MERGE_HEAD`), dirty refusal and
gate-not-applicable; no case writes outside a temp dir; the suite is independent of global git
configuration.
