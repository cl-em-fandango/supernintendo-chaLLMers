# HANDOFF — session 3 (card writing T18–T42) — COMPLETE

**Read this file, then `PLAN-2026-08-26.md`. That is the whole picture. Do not re-read the repo.**

## 0. State

`plan-2026-08-26/` now holds **all 42 card files, T01–T42**. Card writing is finished.
Session 3 wrote `T18`–`T42`. **No source file was edited and nothing was executed** — no `python`, no
`pi`, no `unittest`, no `git` write. Only `read`, `write`, `edit`, and read-only `ls`/`grep`/`wc`.

`git status --short` will show `T13`–`T42` untracked plus the two edited docs
(`PLAN-2026-08-26.md`, `plan-2026-08-26/HANDOVER-PLAN-WRITING.md`) and this file. Committing them is
the operator's call — it was not taken here because committing is an action, not a read.

**Known deviation:** the template in `HANDOVER-PLAN-WRITING.md` §3 asks for 40–65 lines per card;
`T18`–`T42` run 60–85. The overage is the `Verify` heredocs and the *Out of scope* fences, which are
the two sections that keep a card to one session, and the pre-existing `T16`/`T17` are 66/68 by the
same measure. They were not split further: every one is still a single-file, single-judgement-call
change, and splitting an 80-line card usually produces two cards that both need the other's context.
If you want the letter of the rule, trim prose, not verify steps.

`HANDOFF-CONTINUATION.md` (in this directory) is the record of a failed session. Its claims about the
repo and about the plan are **unreliable — do not use it as input.** Its one useful warning stands:
never accept a bare `all good` / `VERDICT: done` from a tool result as evidence; re-derive state from
the filesystem.

## 1. What exists now

| Wave | Cards | Status |
|---|---|---|
| 0 baseline/git safety | T01–T06 | written (session 1) |
| 1 output visibility | T07–T08 | written (session 1) |
| 2 `claimed/` leak | T09–T12 | written (session 1) |
| 3 supervisor loop | T13–T16 | written (session 2) |
| 4 `pi_cli` robustness | T17–T20 | T17 s2, T18–T20 s3 |
| 5 state truthfulness | T21–T25 | s3 |
| 6 checkpoint granularity | T26–T27 | s3 |
| 7 standards adoption | T28–T31 | s3 |
| 8 config & budgets | T32–T33, **T42** | s3 |
| 9 boundary tests | T34–T40 | s3 |
| 10 small items | T41 | s3 |

Renamed cards (index updated in `PLAN-2026-08-26.md`):
`T24-refuse-merge-without-gate.md`, `T25-queue-audit-readonly.md`, `T40-pyproject-and-gate-script.md`.
New card: `T42-over-cap-park-and-handoff.md` (wave 8, depends T32).

## 2. Decisions are resolved — the PLAN file's human notes win over everything

`PLAN-2026-08-26.md` §"Open decisions" carries the answers. They overrode the older guidance in
`HANDOVER-PLAN-WRITING.md` §6; the reconciliation is recorded in that file's §8. In short:

- **D2** — the 60 000-token cap is deliberate (throughput *and* accuracy) and is not to be tuned.
  `modelContext` = real window, new `maxPromptTokens` = the cap (T32), and crossing it ⇒ immediate
  park + markdown handoff, no retry, no judgement (T42). **Do not soften this.**
- **D3** — the per-repo verification gate is "a problem for later". T24 therefore only *refuses*; it
  adds no `verifyCommands` key and detects no toolchain.
- **D4** — the queue stays exactly as it is. T25 is read-only. Anyone who requeues, deletes, or
  "normalizes" `002`/`claimed/`/`auto-3`/`auto-4` is out of scope.
- **D5** — no remote, no CI. T40 ships no `.github/` and no git hook.
- **D6** — the `interrupt`/stand-down command stays out of this plan.

## 3. Fresh facts measured in session 3 (read-only, `grep` over `/home/donald/work/stats/sessions.jsonl`)

These are already embedded in `T28`, `T30` and `T39`; repeated here because they constrain any future
work on verdicts and stages.

```
stage    autonomous_suggest 12 · spec_author 10 · slice_implement 6 · spec_assess_tw 4
         spec_assess_ornith 4 · slice_check 4 · autonomous_review 4 · feasibility 3
         tech_review 2 · smoke 2 · slicing 2 · func_review 2 · smoke32k 1
verdict  unknown 21 · pass 15 · done 14 · error 3 · reject 2 · kickback 1
outcome  done 14 · pass 15 · unknown 23 · error 3 · kickback 1
```

- The wire verdict is **`reject`**, not `rejected`; `Verdict.REJECTED` in `enums.py` does not match
  reality. `_outcome("reject")` → `unknown` today, which is why `reject` 2 but `unknown` outcome 23.
- `smoke` / `smoke32k` (3 rows) are ad-hoc manual runs no code path can emit — deliberately **not**
  enum members.
- `holistic` and `slice_fix` are emitted by code but have **zero** historical rows.
- Row schema: `ts, task_id, stage, model, verdict, outcome, peak_tokens, duration_s, rc,
  prompt_chars, slice, session_file, iteration, notes`. `peak_tokens` is present in all 56 rows —
  that is the signal T42 uses.
- Test suite as measured by inspection: `tests/test_checkpoint_state.py` 13,
  `test_continue_fresh.py` 9, `test_pipeline_resume.py` 8, `test_resume_cli.py` 10 = **40**.
- `core/stats.py` public surface: `SessionRecord`, `StatsStore`, `_group`, `model_report`,
  `stage_report`, `task_report`, `render_report`, `_pct`.

## 4. Next step (do not guess)

Card writing is done; implementation has not started and **T01 has not run**. The next action belongs
to the human: commit `T13`–`T42` + the two edited docs, then run cards in wave order, one fresh
session per card, per the agent contract in `PLAN-2026-08-26.md`.

If asked to *write* more cards: there are none left. If asked to *implement*, start at **T01**, read
the Gate/Rules/decisions in `PLAN-2026-08-26.md`, then the card — and note T42 exists in wave 8.

## 5. Hard rules for whoever picks this up

Never launch `pi` from inside this repo's session; never run the supervisor; never
`git reset --hard`; never write to `/home/donald/work/{queue,stats,logs}`. Tests use temp dirs, temp
git repos and fake `pi` scripts on `PATH` (patterns are in the T10, T12, T35, T36 verify blocks).
Verify blocks are written to be run by the *card-executing* agent, not by a card-writing agent.
