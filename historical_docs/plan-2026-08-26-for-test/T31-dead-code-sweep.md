# T31 — Sweep the dead imports and the enum/string mix left behind

**Wave 7** · depends: T30 · finding: F9

## Context
Wave 7 leaves deliberate debris, and there is also pre-existing debris. Known dead or half-dead
names: `ensure_branch` imported but unused in `task_lifecycle.py` (l.19); `shutil` and
`CheckpointStage` imported but unused in `resume.py` (l.9, l.13); `build` and `CONFIG_PATH` imported
unused in `harness.py` (l.20, l.25); `re` and `shutil` partially used in `pipeline.py`; and
`resume._plan_stages()` (l.24-27) mixing `CheckpointStage` members with the bare string `HOLISTIC` in
one tuple — the one place where the enum is *used* and used wrongly. `_log = print` is duplicated in
`composition.py` and `cli/handlers.py` (T07 may already have taken it — check before acting).

## Read first
- `harness/workflow/resume.py` — the imports and `_plan_stages()`
- `harness/workflow/task_lifecycle.py` l.1-25, `harness.py` l.1-30, `harness/workflow/pipeline.py`
  l.1-25 — the import blocks
- `harness/composition.py` + `harness/cli/handlers.py` — whether T07's log sink removed both `_log`s
- `plan-2026-08-26/T27-merge-checkpoint.md` — it added `CheckpointStage.MERGE`; re-read `_plan_stages`
  in that light

## Do
1. Delete the unused imports listed above. Before each deletion run
   `grep -n "<name>" <file>` and keep the result for the commit message; if the grep shows a use the
   handover missed, leave the import and note it.
2. `_plan_stages()`: make it return `list[CheckpointStage]` — including
   `CheckpointStage.MERGE` if T27 landed — and format the *display* string at the point of printing
   (`stage.value`). The bare `HOLISTIC` string disappears; if the plan preview should still mention
   the holistic review, print it from `Stage.HOLISTIC.value` at the display site, not from the
   checkpoint list.
3. If `_log = print` still exists in both `composition.py` and `handlers.py`, import the T07 log
   sink in both and delete the duplicates. If T07 already did this, say so in the commit message and
   move on — do not restructure the logger.
4. Do not chase warnings beyond this list. In particular do **not** add a linter config here (T40)
   and do not reformat untouched files — a whitespace churn makes the wave-7 diff audit unreadable.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, ast, pathlib; sys.path.insert(0,'.')
targets = {
 'harness/workflow/task_lifecycle.py': ['ensure_branch'],
 'harness/workflow/resume.py': ['shutil', 'CheckpointStage'],
 'harness.py': ['build', 'CONFIG_PATH'],
}
for f, names in targets.items():
    src = pathlib.Path(f).read_text()
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported |= {(a.asname or a.name) for a in n.names}
        elif isinstance(n, ast.Import):
            imported |= {(a.asname or a.name.split('.')[0]) for a in n.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
           {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)}
    for nm in names:
        if nm in imported:
            assert nm in used, f"{f}: {nm} still imported and still unused"
rp = pathlib.Path('harness/workflow/resume.py').read_text()
assert 'HOLISTIC' not in rp.split('def _plan_stages')[1].split('\ndef ')[0], "bare HOLISTIC remains"
print("dead code sweep ok")
PY
python3 -m compileall -q harness external harness.py supervisor.py && echo "compile ok"
```
Must pass, plus the Gate. Commit message must contain the grep table: `name, file, before count,
after count` for every name touched.

## Out of scope
Any behaviour change — this card must not alter a single output line except the resume plan preview's
stage formatting. Also out: the linter/ruff configuration and `pyproject.toml` (T40), the `_log`
*sink* design (T07), `_run`'s all-crash signal and the `_review_loop` bugs (T41), and unused names
you discover elsewhere (report them in the commit message, do not fix them).

## Done when
The four listed files import nothing they do not use; `_plan_stages` returns `CheckpointStage`
members only; `python3 -m compileall` is clean; the Gate passes with the same test count as at card
start; the commit message contains the before/after grep table.
