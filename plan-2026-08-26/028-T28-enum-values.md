# T28 — Correct `Verdict` and `Stage` to the strings actually in use

**Wave 7 (PULLED FORWARD — run it immediately after T19, before T20)** · depends: none · blocks: T20,
T29 · finding: F9

## Context
`core/enums.py` has zero call sites and is actively wrong. Ground truth is
`/home/donald/work/stats/sessions.jsonl` (56 rows, verified 2026-08-27):

```
stage    autonomous_suggest 12 · spec_author 10 · slice_implement 6 · spec_assess_tw 4
         spec_assess_ornith 4 · slice_check 4 · autonomous_review 4 · feasibility 3
         tech_review 2 · smoke 2 · slicing 2 · func_review 2 · smoke32k 1
verdict  unknown 21 · pass 15 · done 14 · error 3 · reject 2 · kickback 1
```

The enum says `IMPLEMENT="implement"`, `SLICE_FIT="slice_fit"`, `HOLISTIC="holistic_review"`; the
code emits `slice_implement`, `slice_check`, `holistic`. `Verdict` has no `KICKOUT` (compared in
`stage_feasibility`), no `UNKNOWN`/`ERROR` (produced by `session.py`), and declares `REJECTED` where
the wire value in the data is **`reject`**. The stats report re-renders those rows on every `report`
run, so the wire strings are load-bearing history.

## Read first
- `harness/core/enums.py` — all 59 lines: `TaskStatus`, `Verdict`, `CheckpointStage`, `Stage`
- `harness/core/session.py` — `_outcome()`'s whitelist (it accepts `kickout`, which the enum lacks)
- `harness/workflow/pipeline.py` — the stage strings passed to `_run`: `spec_author`,
  `spec_assess_ornith`, `spec_assess_tw`, `feasibility`, `slicing`, `slice_check`, `slice_implement`,
  `f"{kind}_review"`, `slice_fix`, `holistic`
- `harness/workflow/autonomous.py` — the `autonomous_*` stage labels it passes

## Do
1. `Stage` — one member per **observed ∪ emitted** value, member name in SCREAMING_SNAKE, value
   byte-identical to the string: `SPEC_AUTHOR="spec_author"`, `SPEC_ASSESS_TW="spec_assess_tw"`,
   `SPEC_ASSESS_ORNITH="spec_assess_ornith"`, `FEASIBILITY="feasibility"`, `SLICING="slicing"`,
   `SLICE_CHECK="slice_check"`, `SLICE_IMPLEMENT="slice_implement"`, `TECH_REVIEW="tech_review"`,
   `FUNC_REVIEW="func_review"`, `SLICE_FIX="slice_fix"`, `HOLISTIC="holistic"`, plus the existing
   `AUTONOMOUS_*` if `autonomous.py` uses them. **Delete** `IMPLEMENT`, `SLICE_FIT`, `HOLISTIC_REVIEW`
   — they never appear in the data or in the code.
2. Do **not** add `smoke` / `smoke32k` to the enum: those 3 rows are ad-hoc manual runs, not a stage
   any code path can produce. Note that in the class docstring — it is the reason the enum is a
   subset of the data.
3. `Verdict` — `PASS, FAIL, KICKBACK, DONE, PROGRESS, RESLICED, INFEASIBLE` as today;
   **add** `KICKOUT="kickout"`, `UNKNOWN="unknown"`, `ERROR="error"`; rename `REJECTED` →
   `REJECT="reject"` **if and only if** `grep -rn "rejected" harness/ external/` shows no consumer
   (check first and paste the grep output in the commit message).
4. Add `NO_VERDICT = "no_verdict"` **unconditionally**. T20's `_map_verdict` returns it for "the
   process finished and said nothing decidable", so a card scheduled after T20 cannot be the one that
   decides whether it exists — that is why this card runs first. `no_verdict` is a new wire value:
   it appears in stats rows from now on and in none of the 56 historical ones, so nothing re-renders
   differently. Note that in the class docstring.
5. Give both enums a `@classmethod parse(cls, raw: str)` helper returning `cls | None` — T20's
   `_map_verdict` and T29's comparisons use it. Keep the classes pure: `enum` import only.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, collections; sys.path.insert(0,'.')
from harness.core.enums import Stage, Verdict
rows = [json.loads(l) for l in open('/home/donald/work/stats/sessions.jsonl')]
hist_stage = {r["stage"] for r in rows} - {"smoke", "smoke32k"}
stage_vals = {m.value for m in Stage}
missing = hist_stage - stage_vals
assert not missing, f"stage values in real data with no enum member: {sorted(missing)}"
assert {"implement", "slice_fit", "holistic_review"} & stage_vals == set(), "stale enum values kept"
assert {"slice_implement","slice_check","holistic","tech_review","func_review","slice_fix"} <= stage_vals
verdict_vals = {m.value for m in Verdict}
for v in {"unknown","pass","done","error","reject","kickback","kickout","no_verdict"}:
    assert v in verdict_vals, f"verdict {v!r} missing"   # no_verdict: T20 returns it, T28 owns it
assert Verdict.parse("pass") is Verdict.PASS and Verdict.parse("nope") is None
assert Stage.parse("slice_implement") is Stage.SLICE_IMPLEMENT
print("enums match reality:", len(hist_stage), "historical stages covered")
PY
```
Must pass, plus the Gate.

## Out of scope
**Do not change any call-site string** — rewriting `slice_implement` to `implement` in `pipeline.py`
would silently rewrite history in the stats report. Replacing the 12 raw comparisons with enum
comparisons is T29; replacing stage strings with `Stage` members is T30; `no_verdict` semantics are
T20's; `_outcome`'s mapping table is T20's (it maps `reject` → `unknown` today — leave it, changing it
changes every rendered report); migrating or rewriting `sessions.jsonl` is out of the plan entirely.

## Done when
Every historical stage value except `smoke`/`smoke32k` has a `Stage` member with an identical value;
`Verdict` covers all six historical verdicts plus `kickout`; no member has a value that appears
neither in the data nor in the code; `sessions.jsonl` is unmodified.
