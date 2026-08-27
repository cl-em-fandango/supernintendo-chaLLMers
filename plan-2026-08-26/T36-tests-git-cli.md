# T36 — Temp-repo tests for `git_cli`: merge, gate, revert, abort, dirty refusal

**Wave 9** · depends: T03, T04, T05, T23, T24, T27 · finding: F11

## Context
`external/git_cli.py` is where the destructive commands live — `merge --squash`, `reset --hard`,
`branch -d`, `tag -f` — and it has zero tests. The revert path was found broken only by inspection
(F6a: `_has()` probed `refs/heads/`), and wave 0 fixed four separate bugs in it (tag lookup, merge
abort, dirty refusal, breaker routing). None of those fixes has a regression test, so the next edit
to this file can silently un-fix them. All of it is testable with throwaway repos and no network.

## Read first
- `external/git_cli.py` — `_git`, `_has`, `ensure_branch`, `merge_to_trunk`, `verify_harness`,
  `_revert_to_last_good`, `is_under_queue` (T23), `gate_applies` / `GateNotApplicable` (T24),
  `cleanup_branch` (T27)
- `plan-2026-08-26/T03-git-tag-lookup.md`, `T04-merge-abort.md`, `T05-dirty-tree-guard.md`,
  `T24-refuse-merge-without-gate.md`, `T27-merge-checkpoint.md` — each names the bug to pin
- `tests/test_checkpoint_state.py` — house style

## Do
1. New file `tests/test_git_cli.py`. Helper `def make_repo(tmp, *, with_harness=False) -> Path`:
   `git init -q -b trunk`, optional `harness.py` + `harness/composition.py` stubs so
   `gate_applies()` can be true without a real harness, one commit, author set with `-c user.email`
   and `-c user.name` on every commit (never rely on global git config).
2. Cases:
   **a.** `_has` finds a **tag** (`pi/last-good`) and does not find a same-named branch that does not
   exist — the exact F6a inversion.
   **b.** `ensure_branch` creates `pi/<id>` from trunk and is idempotent when called twice.
   **c.** `ensure_branch` + `is_under_queue`: a queue path is refused by the workflow guard (mirror
   T23's assertions here so the pair stays tested).
   **d.** happy merge: branch with a change, `gate_applies` true, gate passes (stub `harness.py` that
   exits 0 — read `verify_harness` first and satisfy it with a stub, do not weaken it), trunk moves,
   tag `pi/last-good` updated, and the branch still exists until `cleanup_branch`.
   **e.** gate fails → trunk is back at the pre-merge commit, `pi/last-good` unchanged, exception
   raised, and no `MERGE_HEAD` left behind (T04).
   **f.** conflicting merge → aborts, worktree clean, exception raised (T04).
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
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_git_cli')
assert suite.countTestCases() >= 7, f"only {suite.countTestCases()} cases"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"git_cli tests ok ({suite.countTestCases()} cases)")
PY
```
Must pass, plus the Gate.

## Out of scope
The supervisor's breaker that *calls* git (T38), the verification gate's per-project design (deferred
by **D3** — do not add a `verifyCommands` config key to make testing easier), `pipeline`'s
checkpoint bookkeeping around a merge (T27, already tested there), real remotes or pushes (there is
no remote — D5), and any test that runs against this repo's real `.git`.

## Done when
`tests/test_git_cli.py` has ≥7 green cases covering tag lookup, merge, gate-fail revert, conflict
abort, dirty refusal and gate-not-applicable; no case writes outside a temp dir; the suite is
independent of global git configuration.
