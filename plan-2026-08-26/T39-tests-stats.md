# T39 — Stats tests: the reports over a fixture JSONL

**Wave 9** · depends: none (after T20) · finding: F11

## Context
`core/stats.py` (186 lines) is the only thing the operator actually reads — `harness.py report` — and
it is untested (F11). Its row schema is fixed and small, so the aggregation functions are pure and
trivially testable, and they are the ones most likely to be quietly broken by waves 4/7: T20 changes
which verdicts exist, T29/T30 change what is written at the edge. The live file has 56 rows
(`verdict`: unknown 21, pass 15, done 14, error 3, reject 2, kickback 1; `outcome`: done 14, pass 15,
error 3, kickback 1, unknown 23 — note `reject` maps to `unknown`, which is why a `reject` row and a
`kickback` row are the discriminating fixtures).

## Read first
- `harness/core/stats.py` — `SessionRecord` (l.38), `StatsStore` (l.55), `_group`, `model_report`
  (l.93), `stage_report` (l.125), `task_report` (l.142), `render_report` (l.155), `_pct` (l.185)
- `/home/donald/work/stats/sessions.jsonl` — **read-only** ground truth for the row shape:
  `ts, task_id, stage, model, verdict, outcome, peak_tokens, duration_s, rc, prompt_chars, slice,
  session_file, iteration, notes`
- `tests/test_checkpoint_state.py` — house style

## Do
1. New file `tests/test_stats.py` with an in-file fixture builder: `def row(**kw) -> dict` returning a
   complete row with sensible defaults, and `ROWS = [...]` of 8–12 rows covering: two models, three
   stages, a mix of `pass`/`done`/`error`/`reject`/`unknown`, one row with `rc != 0`, one with
   `duration_s = 0`, one with `peak_tokens` at the cap, and one with `task_id = None`.
2. Cases:
   **a.** `model_report` — per-model counts, success rate and mean duration computed by hand in the
   test (`assert m["sessions"] == 4`, not `assert m["sessions"] > 0`).
   **b.** `stage_report` — counts by stage, and a `reject` row counted where it is counted today
   (assert current behaviour; if it looks wrong, `@unittest.expectedFailure` naming T20, do not fix).
   **c.** `task_report` — `task_id = None` rows are grouped somewhere sane and do not raise.
   **d.** `_pct` — `None`, 0, 1 and 0.5 → whatever the contract is, pinned as strings.
   **e.** `render_report([])` must not raise and must return a string (empty-data path).
   **f.** `render_report(ROWS)` contains each model name and each count you asserted — the
   "aggregation and rendering agree" test.
   **g.** division-by-zero guards: all-error model, all-zero-duration model.
3. `StatsStore` append/read round-trip in a `tempfile.mkdtemp()` path: append 3 rows, read 3 back, in
   order, as dicts. **Never** open `/home/donald/work/stats/sessions.jsonl` for writing — and add an
   assertion in that test that the path under test is not the real one.
4. Do not snapshot the *whole* rendered report as a golden string: the live data changes every run.
   Snapshot small, assert numbers.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, unittest, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_stats.py'); assert p.exists()
src = p.read_text()
assert 'stats/sessions.jsonl' not in src or 'work/stats' in src.split('def ')[0] is False \
       or '/home/donald/work/stats/sessions.jsonl' not in src, "test reads the live JSONL path"
assert 'open(' not in src or 'tempfile' in src or 'mkdtemp' in src
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_stats')
assert suite.countTestCases() >= 7, f"only {suite.countTestCases()} cases"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"stats tests ok ({suite.countTestCases()} cases)")
PY
python3 harness.py report >/dev/null; echo "rc=$?"    # real report still renders, rc=0
```
Must pass, plus the Gate.

## Out of scope
Changing any aggregation formula (this card pins current behaviour — a formula bug is a `@expected‐
Failure` plus a line in the commit message, not a fix here), the verdict→outcome mapping (T20),
writing to the live stats file, new report sections, and CSV/plot output.

## Done when
`tests/test_stats.py` has ≥7 green cases with hand-computed expected numbers; the live
`sessions.jsonl` is unmodified (compare `wc -l` before and after — 56); `harness.py report` still
renders and exits 0.
