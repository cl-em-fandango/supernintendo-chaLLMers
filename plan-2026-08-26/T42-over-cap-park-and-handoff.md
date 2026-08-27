# T42 — Park and hand off the moment context usage crosses the cap

**Wave 8** · depends: T32 · finding: F10 + decision D2 · *(new card, added to the index — D2's answer
created a behaviour the original 41-card plan had no home for)*

## Context
D2, verbatim: "The cap is deliberate and is for both 1) throughput and 2) accuracy. I am hardline on
sticking to this. **The second context usage goes over 60k tokens I want an immediate park and handoff
for next agent via markdown, no questions asked.**" T32 makes the cap explicit and correct, but the
cap is currently only advisory: `model_budget()` sizes the *prompt*, and nothing looks at what a
session actually consumed. `PiSessionResult.peak_tokens` already exists and is already written to the
stats row (`peak_tokens` is in all 56 historical rows) — so the signal is there, unused.
This card makes the cap an enforced stop: over the line ⇒ park the task, write a markdown handoff,
stop burning sessions.

## Read first
- `harness/core/config.py` — `max_prompt_tokens` (T32), `model_budget()`
- `external/pi_cli.py` — where `peak_tokens` is computed from the JSON stream
- `harness/core/session.py` — the stats row write (it already carries `peak_tokens`) and the return
  value into `Pipeline._run`
- `harness/workflow/pipeline.py` — `_run` and the per-stage callers; `task_lifecycle.park(...)` and
  `_exec_summary`

## Do
1. Add `Config.over_budget_limit` = `max_prompt_tokens` (the same number; do not invent a second
   threshold — D2 says one line, 60k).
2. In `Pipeline._run`, immediately after a session returns: if `result.peak_tokens >
   cfg.over_budget_limit` → stop. **Do not retry** (a retry re-reads the same context and is the
   exact cost D2 is trying to avoid) and do not look at the verdict.
   **Shape note for later:** **T41** (wave 10, lands *after* this card) raises a sibling
   `AllAttemptsCrashed(task_id, stage, attempts)` from this same `_run` and handles it in `process()`.
   Define `OverContextBudget` so that second exception is additive — same module, one `except` chain in
   `process()`, one park-with-reason helper shared by both. Do not build a handling pattern T41 would
   have to duplicate; T41's Do 4 says the same thing from its side.
3. Signal it with a named value, not a bool: return/raise `OverContextBudget(peak_tokens, limit,
   stage, task_id)` (a small exception class in `harness/workflow/params.py` or `pipeline.py` — pick
   one, say which in the commit message). Every stage caller treats it the same way, so handle it in
   one place if `_run`'s callers funnel; if they do not, add a decorator/helper rather than editing 8
   call sites by hand.
4. `stage_*` → `pipeline.process()` catches `OverContextBudget` and calls `park(task_id, reason=...)`
   with the numbers in the reason, e.g.
   `context 71204 tokens > cap 60000 at stage=slice_implement slice=3`.
5. `park()` gains the markdown handoff: `_exec_summary` already writes `queue/review/<id>.md`. Extend
   that file for this case with a `## Handoff` section containing: stage + slice + iteration, the
   measured peak vs the cap, what the last session produced (`out_file` path from the result), the
   checkpoint list (`checkpointed_stages`, `checkpointed_slices`), and a
   `## Next agent should` section stating plainly that the task must be re-split or the context
   reduced before resuming. That file *is* the handoff D2 asked for — do not create a second channel.
6. Record the trip in the stats row (`notes` gains `over-cap`), so `report` can count them.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tempfile, pathlib, json; sys.path.insert(0,'.')
from harness.core.config import Config
from harness.workflow import pipeline as PL
from harness.workflow import params          # the other place Do 3 allows the class to live
cfg = Config({"workDir": "/tmp/x", "maxPromptTokens": 60000}, pathlib.Path("/tmp/x/queue"))
assert cfg.over_budget_limit == 60000
# Do 3 allows either module, so the two *different* candidates are the disjunction. (The line this
# replaced repeated `hasattr(PL, ...)` twice and could therefore never fail — PLAN §Rules.)
assert hasattr(PL, "OverContextBudget") or hasattr(params, "OverContextBudget"), \
    "OverContextBudget is in neither pipeline.py nor params.py"
OCB = getattr(PL, "OverContextBudget", None) or getattr(params, "OverContextBudget", None)
assert issubclass(OCB, Exception), "Do 3 asked for a named exception, not a bool or a field"
src = pathlib.Path('harness/workflow/pipeline.py').read_text()
assert 'over_budget_limit' in src and 'peak_tokens' in src, "no cap check in _run"
# the check must sit before the retry loop's next attempt
run = src.split('def _run')[1].split('\n    def ')[0]
assert run.index('over_budget_limit') < run.index('attempt') if 'attempt' in run else True

# --- D2's boundary and D2's "no questions asked" no-retry, both of which Done-when promises and
#     this block used not to test. Three things must hold, all of them able to fail:
#       peak 60001 -> OverContextBudget, and exactly ONE runner call (a retry is the cost D2 bans)
#       peak 60000 -> no exception, exactly ONE runner call (the rule is > cap, never >= cap)
#       the exception carries the measured peak and the cap, because the park reason is built from them
# The setup is scaffolding: build the pipeline the way composition.build() does and call the same
# per-session path _run uses. Adapt the two ADAPT lines to the real signatures — as written they are
# Ellipsis placeholders and the block fails loudly rather than passing vacuously. If driving _run from
# a snippet proves awkward, ship these same assertions as tests/test_over_cap_trip.py inside this
# card (the T23/T27 pattern for behaviour no wave-9 card owns) and say so in the commit message.
class Rec:
    def __init__(self, peak):
        self.peak_tokens, self.verdict, self.out_file = peak, "pass", "/tmp/no-such.out"
class Runner:
    def __init__(self, peak): self.peak, self.calls = peak, 0
    def __call__(self, *a, **k):
        self.calls += 1
        return Rec(self.peak)
def trip(peak):
    r = Runner(peak)
    pipe = ...            # ADAPT: composition-shaped Pipeline, runner=r, cfg with maxPromptTokens 60000
    err = None
    try:
        pipe._run(...)    # ADAPT: the smallest real per-session call (stage / slice / iteration)
    except OCB as e:
        err = e
    return err, r.calls
over, n_over = trip(60001)
assert over is not None, "60001 tokens did not trip the cap"
assert n_over == 1, f"retry happened ({n_over} runner calls) — D2 forbids one over the cap"
at, n_at = trip(60000)
assert at is None, "exactly 60000 tripped: D2's line is 'goes over', not 'reaches'"
assert n_at == 1, f"an in-budget session was run {n_at} times"
assert "60001" in str(over) and "60000" in str(over), \
    "the exception must carry peak and limit — park()'s reason string is built from them"
# handoff text appears in the exec summary for an over-cap park
from harness.workflow.task_lifecycle import TaskLifecycle
root = pathlib.Path(tempfile.mkdtemp())
for d in ("pending","active","parked","failed","done","claimed","review"): (root/"queue"/d).mkdir(parents=True)
c2 = Config({"workDir": str(root), "maxPromptTokens": 60000}, root/"queue")
lc = TaskLifecycle(c2, log=lambda *a: None)
lc.intake("t1","body","test")
lc.park("t1", reason="context 71204 tokens > cap 60000 at stage=slice_implement slice=3")
md = (c2.queue_dir/"review"/"t1.md").read_text()
assert "Handoff" in md and "60000" in md and "Next agent" in md, md[:400]
print("over-cap park + handoff ok")
PY
```
Must pass, plus the Gate.

## Out of scope
The cap value itself (**decided: 60000, D2 — do not tune, do not add per-model caps**), summarizing or
truncating context to *avoid* the trip (that is a design task for after this plan), the `_run`
all-attempts-crashed signal (T41), stats report changes to *display* over-cap trips (T39 may surface
them, this card only writes the note), and anything that resumes or un-parks the task automatically.

## Done when
A synthetic result with `peak_tokens = 60001` parks the task with the numbers in the reason and writes
the `## Handoff` + `## Next agent should` sections into `queue/review/<id>.md`; no retry is attempted
(stub runner counting calls); a result at exactly 60000 does **not** trip. The Verify block now carries
all three of those itself — they used to be claimed only here — plus the `>` -not-`>=` boundary and the
requirement that `OverContextBudget` is one exception class in one of the two modules Do 3 names.
