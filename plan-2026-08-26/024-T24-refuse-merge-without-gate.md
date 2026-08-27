# T24 — Do not merge into a repo the gate cannot verify

**Wave 5** · depends: T23 · finding: F7 · **[decision D3 — recorded, see Context]**

## Context
`verify_harness(workdir)` in `external/git_cli.py` runs `python -c "import harness,
harness.workflow.pipeline, …"` and `python harness.py status` **with `cwd=workdir`**. Those checks
are only meaningful for this repo. For any other repo the import fails, `merge_to_trunk` treats it as
a failed gate, calls `_revert_to_last_good` and raises, and `stage_holistic` parks the task — so a
non-harness task can *never* pass, and every one of them is silently reverted. An undeclared gate is
today a guaranteed fail after a real squash-merge, which is worse than not merging at all.
**D3 answer on record:** "Verification gate will differ based on the project. That is a problem for
later. Do not concern yourself with it." So this card does **not** design a per-project gate. It only
removes the pretence: recognize when the harness gate does not apply and refuse to merge *before*
touching git, instead of merging and reverting.

## Read first
- `external/git_cli.py` — `merge_to_trunk` (checkout trunk → `merge --squash` → commit →
  `verify_harness` → `_revert_to_last_good`) and `verify_harness`
- `harness/workflow/pipeline.py` — `stage_holistic`: `merge_to_trunk(...)` in try/except → park
- `plan-2026-08-26/T04-merge-abort.md`, `T05-dirty-tree-guard.md` — already-landed merge safety, this
  card sits in front of both
- `plan-2026-08-26/T23-no-git-init-in-queue.md` — `is_under_queue`, the predicate to reuse

## Do
1. Add `def gate_applies(workdir: Path) -> bool` to `external/git_cli.py`: true only when the workdir
   is the harness repo itself — the honest test is `(workdir / "harness.py").is_file()` **and**
   `(workdir / "harness" / "composition.py").is_file()`. No config read, no subprocess.
2. In `merge_to_trunk`, check `gate_applies(workdir)` **first**, before `checkout trunk` and before
   `merge --squash`. If it does not apply, raise a distinct, named exception —
   `class GateNotApplicable(RuntimeError)` — with a message that names the workdir and says
   `no verification gate is defined for this repo`. Nothing may have been mutated when it raises:
   assert that in the docstring.
3. In `pipeline.stage_holistic`, catch `GateNotApplicable` separately from the generic
   `except Exception` and park with that message verbatim. The task must not be retried, and the
   branch must still exist for a human (T27 keeps it anyway).
4. Keep `verify_harness` exactly as it is for the harness repo — it is a real gate there and the Gate
   in `PLAN-2026-08-26.md` mirrors it.
5. Write two lines in the module docstring of `git_cli.py`: the gate is harness-only **today**; the
   per-repo gate design is deferred by decision D3 and is not this card's problem.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tempfile, subprocess, pathlib; sys.path.insert(0,'.')
from external import git_cli as G
root = pathlib.Path(tempfile.mkdtemp())
repo = root/"other"; repo.mkdir()
subprocess.run(["git","init","-q","-b","trunk",str(repo)], check=True)
(repo/"f.txt").write_text("x\n")
subprocess.run(["git","-C",str(repo),"add","-A"], check=True)
subprocess.run(["git","-C",str(repo),"-c","user.email=t@t","-c","user.name=t","commit","-qm","init"], check=True)
subprocess.run(["git","-C",str(repo),"branch","pi/t1"], check=True)
head = subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],capture_output=True,text=True).stdout
assert G.gate_applies(repo) is False
assert G.gate_applies(pathlib.Path(".").resolve()) is True
try:
    G.merge_to_trunk(repo, "t1", "trunk", "title"); raise AssertionError("merged with no gate!")
except G.GateNotApplicable as e:
    assert "no verification gate" in str(e)
now = subprocess.run(["git","-C",str(repo),"rev-parse","HEAD"],capture_output=True,text=True).stdout
assert now == head, "repo was mutated before the refusal"
assert not (repo/".git"/"MERGE_HEAD").exists(), "left mid-merge"
st = subprocess.run(["git","-C",str(repo),"status","--porcelain"],capture_output=True,text=True).stdout
assert st.strip() == "", f"dirty after refusal: {st}"
print("gate refusal ok")
PY
```
Must pass, plus the Gate.

## Out of scope
**Designing or configuring a per-repo verification gate — deferred by D3, do not add a
`verifyCommands` config key, do not detect pytest/cargo/npm, do not add a repo manifest.** Also out:
the merge-abort and dirty-tree behaviours (T04, T05), keeping the branch for post-merge resume (T27),
the revert-path fixes (T03, T06), and any change to the harness Gate command list itself.

## Done when
`merge_to_trunk` raises `GateNotApplicable` for a repo without `harness.py` before running any git
write command; `stage_holistic` parks with that reason; the harness repo itself still merges and is
still gated by `verify_harness`; no new key appears in `config.json`.
