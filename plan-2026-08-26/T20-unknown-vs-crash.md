# T20 — Separate a crash from a clean run with no verdict

**Wave 4** · depends: T19, T28 · finding: F5, F9

## Context
`harness/core/session.py` ends with `verdict = _extract_verdict(result.output)` and
`if result.crashed and verdict == "unknown": verdict = "error"`. So today: a crashed session and a
session that finished cleanly but never wrote a verdict line both surface as `"unknown"` to the
pipeline, and `"error"` appears only for the crash-and-silent combination. `unknown` is what the
stages treat as failure, which is how a run that actually *worked* (uppercase verdict — T19 — or a
model that just rambled) gets retried and parked. We need three honest outcomes and no more.

## Read first
- `harness/core/session.py` — the tail of `run()`: verdict, the crashed line, `_outcome()`, and the
  stats row that gets appended
- `harness/core/enums.py` — `Verdict` (currently: pass, fail, kickback, done, progress, resliced,
  infeasible, rejected) and T28's additions
- `harness/workflow/pipeline.py` — `_run()` (retries `max_crash_retries + 1` times and returns the
  last result regardless) and the two or three places a verdict routes control flow

## Do
1. Depends on **T28** having added `Verdict.UNKNOWN = "unknown"`, `Verdict.ERROR = "error"`,
   `Verdict.NO_VERDICT = "no_verdict"`. If they are absent, STOP — T28 has not landed; do not add them
   here. This is why T28 is **pulled forward** out of wave 7 and scheduled immediately before this
   card (see `PLAN-2026-08-26.md` §Dependency graph): a wave-4 card cannot depend on a wave-7 card and
   still be runnable in wave order.
2. Mapping, applied in this order and written as a small named helper `_map_verdict(crashed: bool,
   parsed: str) -> Verdict` so it is testable without a subprocess:
   - `crashed` → `Verdict.ERROR` (a crash is a crash even if the child printed `VERDICT: pass`
     into a half-written buffer).
   - not crashed, parsed `"unknown"` (i.e. no verdict line at all) → `Verdict.NO_VERDICT`.
   - not crashed, parsed value in the `Verdict` vocabulary → that member.
   - not crashed, parsed value **not** in the vocabulary (e.g. `kick_out`) → `Verdict.UNKNOWN`,
     and keep the raw text in the stats `notes` so we learn the real vocabulary.
3. `_outcome()` must keep returning the same strings it returns today for the verdicts present in
   the 56 historical rows (`pass fail kickback done progress resliced error`) — those rows are
   re-rendered by the stats report and must not change meaning. It additionally accepts `kickout`
   (T28 adds the member) and the two new values; `no_verdict` maps to the same outcome bucket as
   `unknown` did, so routing is unchanged.
4. Say in the docstring which is which, in one sentence each: `error` = the process did not finish;
   `no_verdict` = it finished and said nothing decidable; `unknown` = it said something outside the
   vocabulary.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, collections, pathlib; sys.path.insert(0,'.')
from harness.core.enums import Verdict
from harness.core import session as S
m = S._map_verdict
assert m(True, "unknown") is Verdict.ERROR
assert m(True, "pass") is Verdict.ERROR            # crash wins over a partial verdict line
assert m(False, "unknown") is Verdict.NO_VERDICT
assert m(False, "pass") is Verdict.PASS
assert m(False, "kickout") is Verdict.KICKOUT
assert m(False, "kick_out") is Verdict.UNKNOWN     # outside the vocabulary
# historical rows must still render unchanged
rows = [json.loads(l) for l in open('/home/donald/work/stats/sessions.jsonl')]
hist = collections.Counter(r.get('verdict') for r in rows)
for v in hist:
    assert v is None or S._outcome(v) is not None, f"{v} no longer maps"
assert S._outcome("pass") and S._outcome("resliced") and S._outcome("error")
print("verdict mapping ok", dict(hist))
PY
```
Must pass, plus the Gate.

## Out of scope
The regex itself (T19), adding the enum members (T28 — this card *consumes* them), the 12 raw
`verdict == "..."` comparisons in `pipeline.py` (T29), `_run`'s "all attempts crashed" signal (T41),
the hard 60k context cap and its park-and-handoff behaviour (T42), and any migration of
`/home/donald/work/stats/sessions.jsonl` — read it, never rewrite it.

## Done when
`_map_verdict` is a pure helper with the four rules above; no code path can produce `"unknown"` for
a crashed session or `"error"` for a clean-but-silent one; the historical verdict vocabulary in the
stats file still maps; the Gate's report render is unchanged.
