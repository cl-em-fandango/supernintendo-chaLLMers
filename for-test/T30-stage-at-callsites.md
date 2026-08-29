# T30 — Compare stage names as `Stage` members, including the f-string ones

**Wave 7** · depends: T29 · finding: F9

## Context
`pipeline.py` passes raw stage strings to `_run` / `SessionRunner.run`, which writes them verbatim
into `sessions.jsonl`. Eleven distinct values are produced in code: `spec_author`,
`spec_assess_ornith`, `spec_assess_tw`, `feasibility`, `slicing`, `slice_check`, `slice_implement`,
`tech_review` and `func_review` (built as `f"{kind}_review"`), `slice_fix`, `holistic`. Two are
built by concatenation — a typo there is invisible to a reader and to grep — and one value
(`slice_fix`) is shared by both fix kinds, which is a fact worth naming in code rather than
discovering. The stats report re-renders the historical rows, so a changed value is a silent history
rewrite.

## Read first
- `harness/core/enums.py` — `Stage` as corrected by T28
- `harness/workflow/pipeline.py` — every `_run(...)` call and its `stage=` argument, `_review_loop`
  (the `kind` parameter and its f-strings), `stage_holistic`
- `harness/workflow/autonomous.py` — its own stage labels (`autonomous_suggest`, `autonomous_review`)
- `harness/core/session.py` — how `stage` reaches the stats row (`.value` needed at the edge)

## Do
1. Replace every `stage="..."` argument with the `Stage` member from T28. No new values.
2. `_review_loop`: stop building `f"{kind}_review"`. Take the stage explicitly —
   `_review_loop(..., stage: Stage, ...)` — and pass `Stage.TECH_REVIEW` / `Stage.FUNC_REVIEW` from
   the two call sites. If `kind` is still needed for anything else, keep it as an enum, not a string
   (`class ReviewKind(str, Enum)` belongs in `enums.py` if, and only if, a use remains).
3. `slice_fix`: both call sites become `Stage.SLICE_FIX`. Do **not** split it into
   `fix_tech`/`fix_func` — the data has no such values and splitting changes the report. If a
   distinction is wanted later, that is a new stage value and a human decision.
4. At the stats edge only (`session.py` → `stats`), convert with `stage.value if isinstance(stage,
   Stage) else stage` — one place, tolerant, so a stray string still records rather than raising.
5. `autonomous.py`'s stage labels become `Stage.AUTONOMOUS_SUGGEST` / `Stage.AUTONOMOUS_REVIEW`
   (T28 kept those members; values `autonomous_suggest` / `autonomous_review` — 12 and 4 historical
   rows depend on them).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, re, json, pathlib; sys.path.insert(0,'.')
from harness.core.enums import Stage
vals = {m.value for m in Stage}
need = {"spec_author","spec_assess_ornith","spec_assess_tw","feasibility","slicing","slice_check",
        "slice_implement","tech_review","func_review","slice_fix","holistic",
        "autonomous_suggest","autonomous_review"}
assert need <= vals, f"missing members: {sorted(need - vals)}"
for f in ("harness/workflow/pipeline.py","harness/workflow/autonomous.py"):
    src = pathlib.Path(f).read_text()
    bad = re.findall(r'stage\s*=\s*["\'][a-z_]+["\']', src)
    assert not bad, f"{f}: raw stage literals {bad}"
    assert '_review}' not in src, f"{f}: f-string-built stage still present"
# byte-identity: a synthetic row must carry the same stage string as before the change
row = json.loads(json.dumps({"stage": Stage.SLICE_IMPLEMENT.value}))
assert row["stage"] == "slice_implement"
print("stage call sites ok")
PY
python3 -m unittest discover -s tests
```
Must pass, plus the Gate.

## Out of scope
`Verdict` comparisons (T29), the `_outcome` table (T20), adding `smoke`/`smoke32k` or inventing
`fix_tech`/`fix_func` values (T28's rule: the enum is observed reality, not aspiration), renaming any
stage for readability, `resume._plan_stages`' enum/string mix and unused imports (T31), and anything
under `/home/donald/work/stats/`.

## Done when
`grep -n 'stage="' harness/workflow/*.py` prints nothing; no f-string builds a stage; a test asserts
the exact wire string `"slice_implement"` survives a synthetic run; the report output for the
existing 56 rows is unchanged.
