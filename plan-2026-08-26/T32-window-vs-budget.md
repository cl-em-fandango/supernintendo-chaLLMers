# T32 — Split the context *window* from the prompt *cap*

**Wave 8** · depends: none · finding: F10 · **[decision D2 — recorded, see Context]**

## Context
`config.json`'s `modelContext` holds **budgets**, not windows (`QwenOptimised64k: 60000`,
`…128k: 60000`, `Qwen3.8-DFLASH2-*: 60000`, `*32k: 32768`), so `model_context()` returns a lie and
`model_budget()` then subtracts an 8192 reserve on top: measured, a 128k model is told **51k**,
`QwenOptimised64k` (real 65536) is told 51k, `QwenOptimised32k` is told 24k. `load()`'s `tokenBudget`
default is `100_000` while `config.json` sets `60000` and the README says "default 60k".

**D2 answer on record:** the 60k cap is **deliberate**, for throughput *and* accuracy — "I am hardline
on sticking to this. The second context usage goes over 60k tokens I want an immediate park and
handoff for next agent via markdown, no questions asked." So: the cap stays at 60000 and is a *cap*;
the *window* is a separate, true number. The over-cap **park + markdown handoff** behaviour is T42
(depends on this card) — do not implement the trip here.

## Read first
- `harness/core/config.py` — `load()` (the `tokenBudget` default at ~l.80), `model_context(m)`
  (map hit → `32k/64k/128k` name suffix → `131072`), `model_budget(m)`, `get()`
- `config.json` — `tokenBudget: 60000`, the `modelContext` map, `models.*`
- `harness/core/session.py` — `full_prompt = prompts.CONTEXT_BUDGET_NOTE.format(budget_k=budget //
  1000) + prompt` and the log line that prints `budget={budget}k` (units are T33's, not yours)
- `plan-2026-08-26/T42-over-cap-park-and-handoff.md` — the consumer of what you expose

## Do
1. Two concepts, two keys. `modelContext` becomes the **real window** per model
   (`QwenOptimised32k: 32768`, `QwenOptimised64k: 65536`, the 128k entries: `131072`), and a new key
   `maxPromptTokens` is the **working cap** — set it to `60000` (the value D2 fixed) and give
   `load()` a default of `60_000`, not `100_000`.
2. `model_context(m)` keeps its name (call sites and the README both use it) and becomes truthful:
   map hit → that window; name-suffix heuristic → as today; otherwise `131072` **and** log a warning
   `unknown context window for <m>, assuming 131072`. Rename nothing.
3. `model_budget(m) = max(4096, min(max_prompt_tokens, model_context(m) - reserve))` where `reserve`
   stays 8192. With the D2 config this yields 60000 for a 128k model and 57344 for the 64k model —
   write those two numbers in the docstring so the next reader can check the arithmetic by hand.
4. Add `Config.max_prompt_tokens` (property) and `Config.model_window(m)` as an explicit alias of
   `model_context` **only if** it improves readability at a call site; otherwise do not add it.
5. Update `config.json` in place, keep key order, add the trailing newline (also T33's, whoever gets
   there first — it is idempotent). **Do not change any prompt text.**

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, json; sys.path.insert(0,'.')
from harness.core.config import load, Config
cfg = load("config.json")
assert cfg.max_prompt_tokens == 60000, cfg.max_prompt_tokens
assert cfg.model_context("QwenOptimised64k") == 65536, cfg.model_context("QwenOptimised64k")
assert cfg.model_context("QwenOptimised32k") == 32768
assert cfg.model_budget("QwenOptimised64k") == 57344, cfg.model_budget("QwenOptimised64k")
b128 = cfg.model_budget("GPT-OSS-120B")
assert b128 == 60000, f"cap not applied: {b128}"
assert b128 <= cfg.max_prompt_tokens
raw = json.loads(open("config.json").read())
assert raw["maxPromptTokens"] == 60000
assert all(v >= 32768 for v in raw["modelContext"].values()), "modelContext still holds budgets"
assert open("config.json").read().endswith("\n")
print("window vs cap ok:", {m: (cfg.model_context(m), cfg.model_budget(m))
                            for m in ("QwenOptimised32k","QwenOptimised64k","GPT-OSS-120B")})
PY
```
Must pass, plus the Gate.

## Out of scope
The over-cap **trip** — park + markdown handoff, "no questions asked" (that is T42, it depends on
this one). Also out: the `budget={n}k` unit bug and the README budget section (T33), `maxCrashRetries`
in `config.json` (T33), raising or lowering the 60000 cap (**decided, do not revisit**), and any
change to `CONTEXT_BUDGET_NOTE`'s wording.

## Done when
`modelContext` contains windows only; `maxPromptTokens` = 60000 exists and is the effective cap for
every model whose window exceeds it; a 64k model budgets 57344; `python3 harness.py status` and the
Gate still pass; no prompt string changed (`git diff` shows no change under `harness/core/prompts.py`).
