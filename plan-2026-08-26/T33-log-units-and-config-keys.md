# T33 — Fix the `k` units, name the missing config key, re-sync the docs

**Wave 8** · depends: T32, **T41** (README documents `resume --fresh`, which T41 adds) ·
findings: F10, F12, F14

## Context
`session.py` logs `budget={budget}k ctx={...}k` where both values are **raw token counts** — off by
1000×, so the log says `budget=60000k` for a 60 000-token budget. `pipeline.py` reads
`cfg.get("maxCrashRetries", 2)` but `maxCrashRetries` is absent from `config.json`, so a real
behavioural knob is an invisible default. `config.json` has no trailing newline. And the docs have
drifted hard: README references the deleted `./supervisor.sh` and `harness/providers.py`, omits
`claimed/` from the layout, documents no `resume` / `--continue` / `--fresh` / `run-one` /
`run-task-loop`, and claims `work/logs/harness.log` is written (true only since T07);
`REFACTOR_PLAN.md` and `refactor-tasks/README.md` still say automation does not run until chunk 7 —
automation has already run 56 sessions.

**Path correction (post-audit move):** both now live under `historical_docs/`
(`historical_docs/REFACTOR_PLAN.md`, `historical_docs/refactor-tasks/README.md`) — there is no
root-level `REFACTOR_PLAN.md` or `refactor-tasks/` any more and the audit's paths for them are stale.
Edit them where they are; do not re-create anything at the old paths.

## Read first
- `harness/core/session.py` — the `budget=…k ctx=…k` log line (~l.63)
- `harness/core/config.py` + `config.json` — what keys actually exist after T32
- `README.md` — command reference and token-budget sections (`python3 harness.py --help` is the
  authority on what may be documented)
- `historical_docs/REFACTOR_PLAN.md`, `historical_docs/refactor-tasks/README.md` — the stale
  "not started" claims

## Do
1. Log real units: `budget={budget} tokens ctx={ctx} tokens` — or `budget={budget/1000:.1f}k` if the
   surrounding lines are already `k`-style. Pick one, apply it consistently, and never print a raw
   count with a `k` suffix again.
2. Add `"maxCrashRetries": 2` to `config.json` explicitly (same value as the code default, so this is
   a truth-forcing change, not a behaviour change) and give the file its trailing newline.
3. README: replace `./supervisor.sh` with the `python3 supervisor.py …` reality; fix
   `harness/providers.py` → `harness/core/providers.py`; add `claimed/` to the layout with one line on
   what it means and which command returns stale claims (`requeue-claims`, T12); document `resume`,
   `--continue`, `run-one`, `run-task-loop`, `queue-audit` (T25) — and `resume --fresh` **only if T41
   has already landed** (T33 depends on T41 precisely so the README never advertises a flag
   `cli/parser.py` lacks; check with `python3 harness.py resume --help`, and if it is absent leave it
   out and note it as a follow-up); state the
   budget truth as "cap `maxPromptTokens` = 60000 tokens (deliberate, D2), windows in
   `modelContext`"; and say `work/logs/harness.log` is written by the log sink.
4. `historical_docs/REFACTOR_PLAN.md` + `historical_docs/refactor-tasks/README.md`: replace the "no
   automation until chunk 7 / not started until Task 7" claims with the current state and a pointer to
   `PLAN-2026-08-26.md` as the live index.
5. If this card is running long, **stop after step 4** and leave task-002 slice-4's EC12 doc note
   (`resume` preserves checkpoints, `unpark` does not) as a follow-up line in the commit message
   rather than growing the card past 65 lines.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json, pathlib, re, subprocess; sys.path.insert(0,'.')
s = pathlib.Path('harness/core/session.py').read_text()
assert not re.search(r'budget=\{[a-z_.]+\}k', s), "raw count printed with a k suffix"
assert not re.search(r'ctx=\{[a-z_.]+\}k', s), "raw ctx with a k suffix"
raw = open('config.json').read()
assert json.loads(raw)["maxCrashRetries"] == 2
assert raw.endswith("\n"), "config.json still has no trailing newline"
r = pathlib.Path('README.md').read_text()
assert 'supervisor.sh' not in r, "README still points at the deleted shell supervisor"
assert 'harness/providers.py' not in r
for tok in ("claimed", "resume", "--continue", "run-one", "run-task-loop"):
    assert tok in r, f"README does not document {tok}"
assert 'harness.log' in r
for f in ("historical_docs/REFACTOR_PLAN.md", "historical_docs/refactor-tasks/README.md"):
    assert pathlib.Path(f).is_file(), f"{f} missing — locate it, do not create it at the old path"
    t = pathlib.Path(f).read_text()
    assert 'Not started until Task 7' not in t, f"{f} still claims no automation"
if 'resume --fresh' in r:      # never advertise an unlanded flag
    h = subprocess.run([sys.executable, "harness.py", "resume", "--help"], capture_output=True, text=True)
    assert '--fresh' in h.stdout, "README advertises resume --fresh; the parser does not have it"
print("units + config + docs ok")
PY
```
Must pass, plus the Gate.

## Out of scope
Budget *semantics* (T32 landed them — do not change `model_budget` arithmetic here), the over-cap trip
(T42), `harness/core/prompts.py` wording, rewriting the refactor task cards beyond the stale-status
lines, and adding a docs site/build step.

## Done when
No log line prints a raw token count with a `k`; `config.json` has `maxCrashRetries` and a trailing
newline; README names every subcommand in `cli/parser.py` (check with `python3 harness.py --help`)
and contains no deleted paths; the two refactor docs no longer claim automation is unstarted.
