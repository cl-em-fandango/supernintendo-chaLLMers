# HANDOVER — plan writing, session 1 → session 2

**You are a fresh agent with no memory.** This file + `PLAN-2026-08-26.md` +
`AUDIT-2026-08-26.md` is the whole picture. Do **not** re-read the repo: §5 is the code
intelligence already extracted and verified in session 1.

## 1. Mission remaining

The plan index exists (41 cards). Card **files** exist for **T01–T12 only**.

**Your job: write `plan-2026-08-26/T13-*.md` … `T41-*.md`.**

You are NOT implementing findings. Each card must be executable cold by a *different*
single-session, small-context agent. Do not fix code — if you edit anything outside
`plan-2026-08-26/`, stop.

**Token stop-rule:** approaching 80k, stop writing, update §8 Progress, and emit a further
handover for the next session. Waves are independent enough to split again.

Suggested batching (stop-check between passes):

| Pass | Cards |
|---|---|
| 1 | T13–T20 (waves 3–4: supervisor loop, `pi_cli`) |
| 2 | T21–T31 (waves 5–7: state truth, checkpoints, enums) |
| 3 | T32–T41 (waves 8–10: config, tests+CI, small items) |

## 2. Tree state (session 1 — re-check with `git status`)

- `/home/donald/work/harness`, baseline `pi/trunk` = `pi/last-good` = **`18f8a2c`**.
- `git status --short`: ` M README.md`, ` M config.json`, ` D supervisor.sh`,
  `?? AUDIT-2026-08-26.md`, `?? PLAN-2026-08-26.md`, `?? plan-2026-08-26/`.
- 40 tests pass (`python3 -m unittest discover -s tests`, ~1.7 s). Python 3.14.7.
- **Nothing is implemented. No finding is fixed. T01 has not run.**
- Written in session 1: `PLAN-2026-08-26.md`, `plan-2026-08-26/T01..T12`, this file.

## 3. Card template — copy verbatim, keep every heading

````markdown
# T<nn> — <imperative title, one clause>

**Wave <n>** · depends: <Txx|none> · `[tag]` if it may advance pi/last-good · finding: F<x>

## Context
<3-6 lines: what is broken, verified evidence (file:line / live observation), why it matters.>

## Read first
<2-4 exact paths + function/line range. Never "explore the repo".>

## Do
<3-6 numbered steps, each one mechanical change. Name new functions/files.>

## Verify
```bash
cd /home/donald/work/harness
<copy-paste runnable. Prefer `python3 - <<'PY'` asserting the bug is gone against a TMP repo
or TMP queue, printing "<thing> ok". Never touch /home/donald/work/queue or the real repo in a
verify block. Never spawn a real pi session or the supervisor loop.>
```
All must pass, plus the Gate.

## Out of scope
<Adjacent things an eager agent might also "fix", and who owns them. Most important section —
this is what keeps a card to one session.>

## Done when
<2-3 objectively checkable facts.>
````

Card length 40–65 lines. Longer → split it. Shorter → under-specified.

Mechanics that worked in session 1: write several cards per tool call with
`cat > f.md <<'EOF' … EOF`, and put a nested `python3 - <<'PY'` *inside* the card — a quoted
outer delimiter means the nested heredoc is just text and survives intact.

## 4. The Gate (reference it, do not restate per card)

```bash
cd /home/donald/work/harness
python3 -m unittest discover -s tests        # >= count at card start, 0 failures
python3 -c "import sys; sys.path.insert(0,'.'); import harness, harness.workflow.pipeline, harness.workflow.autonomous, harness.core.session, harness.core.providers, harness.core.gitops, harness.core.stats, external.pi_cli, external.git_cli; print('import ok')"
python3 harness.py status ; echo "rc=$?"     # rc=0
```

Commit: `git add -A && git -c user.email=pi@harness.local -c user.name=pi-harness commit -m "…"`;
`git tag -f pi/last-good pi/trunk` only on `[tag]` cards.

Binding rules (CODING_STANDARDS.md): `external/` owns **all** subprocess; `cli/` parses and
dispatches only; `workflow/` composes; `harness.py`/`composition.py` wire only; direction
`cli → workflow → core → external`. Enums for discrete state inside our code, raw strings only
at process edges. No dicts/tuples for meaningful state. Behavior-preserving unless the card says
the behavior is wrong.

## 5. Code intelligence (verified in session 1 — the reason you need not re-read)

**Sizes.** `harness.py` 58 · `composition.py` 28 · `cli/parser.py` 66 · `cli/handlers.py` 166 ·
`workflow/pipeline.py` 308 · `task_lifecycle.py` 238 · `resume.py` 104 · `continue_fresh.py` 67 ·
`autonomous.py` 98 · `params.py` 11 · `core/prompts.py` 263 · `stats.py` 186 · `config.py` 113 ·
`providers.py` 108 · `session.py` 123 · `enums.py` 59 · `gitops.py` 9 (re-export) ·
`external/pi_cli.py` 176 · `external/git_cli.py` 108 · `supervisor.py` 301 · `tests/` 4 files ≈900.

**`composition.build()` → 5-tuple `(cfg, store, runner, provider, pipeline)`**; mkdirs
`("pending","active","done","failed","parked","review")` — **not `claimed`**. `_log = print`
is duplicated in `composition.py` and `cli/handlers.py`.

**`cli/handlers.py`** — `cmd_run(file)` builds a `Task` from a path; `cmd_run(continue_)`
(l.44-56) `fetch_pending(claim=True)` (claims ALL) → python loop → autonomous when empty → **no
requeue** (the F2 leak); `cmd_run_one` (l.63-83) claims all, processes `[0]`, `_requeue_claimed`
for `[1:]`; `cmd_run_task_loop(continue_)` (l.86-105) whose docstring (l.92-96) *claims the
supervisor calls it with `--continue`* — it does not; `cmd_status` (l.114) iterates the six dirs,
no `claimed`; `cmd_resume(task_id, yes)`; `cmd_unpark(task_id)` moves `parked|failed` →
`pending/<id>.md`, `shutil.rmtree` the old dir, unlinks `review/<id>.md`. `_slug` duplicated here too.

**`cli/parser.py`** subcommands: `run(--continue)`, `run-task <file>(--continue,--fresh)`,
`run-one`, `run-task-loop(--continue)`, `autonomous`, `status`, `report`, `resume <id>(--yes/-y)`,
`unpark <id>`, `requeue` (hidden alias → unpark). No `--fresh` on `resume`; no `requeue-claims`.

**`supervisor.py`** — constants `LOG=WORK_DIR/logs/supervisor.log`, `PIDFILE`, `STOPFILE`,
`SLEEP_S` (env, 60), `MAX_CYCLES`, `FAIL_LIMIT` (3). `log()` appends forever (T02). Lock helpers
(`acquire_lock/_pid_alive/read_pid/release_lock`) and `daemonize` are fine.
`ChildTracker.spawn` (l.110-119) uses `stdout=DEVNULL, stderr=DEVNULL, start_new_session=True`
then `.wait()`; `kill_tree()` does `os.killpg` SIGTERM→SIGKILL — **keep `start_new_session`**.
`run_loop()` (l.148+) per cycle: STOPFILE → MAX_CYCLES → breaker `spawn([py,"harness.py","status"])`;
on rc≠0 `failcount+=1`, at FAIL_LIMIT runs **inline** `subprocess.run(["git","reset","--hard",
"pi/last-good"])` + `["git","rev-parse","--short","pi/last-good"]` (T06), `_sleep`, `continue`;
else `provider=create_provider(load(CONFIG_PATH))`, `pending=provider.fetch_pending()` (read-only),
`log(f"── cycle {cycle}: pending={len(pending)} ──")`, then **`if pending: run-one / else:
autonomous`** — F1: `active/` and `claimed/` are never consulted. `_sleep(stop, secs)` is a real
1 Hz interruptible sleep, so the historical "[DRY] run-task-loop --continue spin storm" came from
the *deleted* `supervisor.sh`. **Be truthful in T15:** the fix is no-progress detection +
progressive backoff (a wedged cycle still burns a full status+work spawn, and log lines, every
60 s forever), not "add a missing sleep".

**`external/pi_cli.py`** — `HEARTBEAT_S=30`, `HARD_TIMEOUT_S=5400`;
`run_pi_session(*, model, workdir, prompt, out_file, log) -> PiSessionResult(rc, crashed, err,
peak_tokens, duration_s, output, out_file)`. Popen `["pi","--provider","llama-swap","--model",m,
"--no-session","--mode","json","-p",prompt]`; **`stderr=PIPE` is never drained until l.117, after
the stdout loop** (F5 deadlock); `deadline=t0+HARD_TIMEOUT_S` is checked **only inside
`for line in proc.stdout`** (a silent child never times out); heartbeat = daemon thread using
`stop_hb.wait(HEARTBEAT_S)` — reuse that shape for a watchdog; parses `message_end`
(`usage.totalTokens`, assistant `content[].text`) and `agent_end`; `err` gets stderr's last 2000
chars and then `output += f"\n[stderr]\n{err}"` — **so stderr text is inside the string the verdict
regex runs over**; `_extract_verdict` = `VERDICT:\s*([a-z_]+)` (lowercase only ⇒ `VERDICT: DONE`
→ `unknown`), fallback `"verdict"\s*:\s*"([a-z_]+)"`, else `"unknown"`; `_now()`.
`pi` is at `/usr/local/bin/pi`; flags used are all valid.

**`core/session.py`** — `SessionRunner(cfg, store, log).run(model, workdir, prompt, *, task_id,
stage, slice_id, iteration, notes)`. `out_file = workdir/f".pi-session-{stage}-{ts}.out"` → lands in
the workdir (F7/F13 strays). Log l.63-64: `f"budget={budget}k ctx={...}k"` printing **raw token
counts with a `k` suffix — wrong by 1000×** (F10). `full_prompt =
prompts.CONTEXT_BUDGET_NOTE.format(budget_k=budget//1000) + prompt`. `verdict =
_extract_verdict(result.output)`; `if result.crashed and verdict=="unknown": verdict="error"`.
`_outcome(verdict)` whitelists `pass,fail,kickback,kickout,done,progress,resliced,error` —
`kickout` is **not in `Verdict`**, and neither is `unknown` (F9).

**`core/config.py`** — `load()` keys: `workDir`, `tokenBudget` (default **100_000**),
`maxSpecKickbacks` 3, `maxSliceImplement` 5, `maxSliceTechReview` 5, `maxSliceFuncReview` 5,
`maxSliceCheckLoops` 3, `autonomousQueueTarget` 5, `trunkBranch` `"pi/trunk"`, `taskProvider`,
`directoryProvider`, `models`, `modelContext`. Properties: `queue_dir`, `logs_dir`,
`sessions_dir`, `stats_path`, `model`(=technicalWriter), `implementer`, `assessor`, `random_pool`,
`fast_pool` (`fastPool` else MOE/A3B-name heuristic). `model_context(m)`: map hit → else name
suffix `32k/64k/128k` → else `131072`. `model_budget(m) = max(4096, min(token_budget,
model_context(m) - 8192))`. `get(key, default)` reads `raw`.

**`config.json` (uncommitted!)** — `workDir=/home/donald/work`, `tokenBudget=60000`, `modelContext`
holds **budgets, not windows** (`QwenOptimised64k:60000`, `…128k:60000`, `Qwen3.8-DFLASH2-*:60000`,
`*32k:32768`), `models.{technicalWriter,implementer,assessor,fastPool[7],randomPool[12]}`,
**no `maxCrashRetries`** (pipeline silently defaults 2), **no trailing newline**. Measured (F10):
128k model → prompt says **51k**; `QwenOptimised64k` (set 60000, real 65536) → **51k**;
`QwenOptimised32k` → **24k**. README says "default 60k" while `load()` defaults 100_000.

**`workflow/pipeline.py`** — `STAGE_SEQUENCE` = SPEC,FEASIBILITY,SLICING,SLICES;
`_STAGE_FUNCTIONS` maps each to a method-name string. `Pipeline(cfg, runner, log=print,
provider=None)`; `max_crash_retries = cfg.get("maxCrashRetries", 2)`. `_run(model, workdir,
prompt, *, task_id, stage, **kw)` retries `runner.run` `max_crash_retries+1`× and **returns the
last result even if every attempt crashed** (F14). `process(task)`: `task.json` exists → resume
from `checkpointed_stages`, else `intake`; `provider.release_claim(task)`;
`workdir = resolve_workdir(task_dir)`; `ensure_branch(workdir, task.id, cfg.trunk_branch)` (park on
exception); loop `STAGE_SEQUENCE` skipping checkpointed with `set_stage` → `getattr(self,
_STAGE_FUNCTIONS[stage])(ctx)` → `checkpoint`; then `stage_holistic(ctx)`. `_stage_failed`:
feasibility → failed only if the dir is gone, else parked. Raw stage strings passed to `_run`:
`"spec_author"`, `"spec_assess_ornith"`, `"spec_assess_tw"`, `"feasibility"`, `"slicing"`,
`"slice_check"`, `"slice_implement"`, `f"{kind}_review"` (→ `tech_review` / `func_review`),
`"slice_fix"` (one value for both kinds), `"holistic"`. `_review_loop` (l.236-263):
`model = self.cfg.implementer if kind=="tech" else self.cfg.model` → **the functional fix session
runs on the technical-writer model** (F14), and writes feedback to
`artifacts/progress/slice-{sid}.md`, **the same path `_implement` uses for its progress note**
(F14 collision). `stage_holistic`: `pass` → `merge_to_trunk(...)` in try/except (park on fail) →
`complete(...)`; else park. **12 raw `verdict == "..."` comparisons.** `_parse_slices` reads
`^### Slice <n>(.n)` from `artifacts/slices.md`; `_summary` scrapes `## Summary`.

**`workflow/task_lifecycle.py`** — `QUEUE_LOCATIONS = ("active","parked","failed","done")`.
`TaskState(id, status, source, created, stage, history, checkpointed_stages, last_updated)` +
`to_json`. `write_atomic` (tmp + `os.replace`), `_now()` (UTC iso, seconds), `_parse_stages` (drop
unknown with warning, dedupe keep-order). `TaskLifecycle(cfg, log=print)`: `task_dir`,
`task_json_path`, `load_state` (tolerant of corrupt/old), `save_state`, `checkpoint` (idempotent
ordered append), `set_stage`, `intake`, **`park`/`fail`/`complete` = `shutil.move` +
`_exec_summary` only — never rewrite `status`** (F4), `_exec_summary` → `queue/review/<id>.md`,
`resolve_workdir(task_dir)` = regex `/[a-zA-Z0-9_./-]+` over `original.md`, first hit that is a dir
containing `.git`, else **the task dir itself** (F7). Unused import `ensure_branch` (l.19).

**`workflow/continue_fresh.py`** (67) — `in_flight_task_dirs` (active/ dirs that have `task.json`;
orphans left alone), `task_from_dir` (id=dirname, body=`original.md`|"", source=state.source|"resume"),
`resume_in_flight(lifecycle, pipeline, log)` → `process()` each, returns count;
`fresh_restart(task_id, cfg, log)` rmtree `active/<id>` + unlink `review/<id>.md`.

**`workflow/resume.py`** (104) — `resume_task(task_id, yes, cfg, pipeline, *, lifecycle, log)`;
search order active→parked→failed→done; plan preview + `[Y/n]`/`--yes`; `_plan_stages()` (l.24-27)
**mixes `CheckpointStage` members with the bare string `HOLISTIC`**; unused imports `shutil` (l.9),
`CheckpointStage` (l.13); reconstructs the `Task` for `process()`. No `--fresh`.

**`core/enums.py`** — `TaskStatus(str,Enum)` PENDING/ACTIVE/DONE/PARKED/FAILED (effectively
decorative, F4). `Verdict` = pass, fail, kickback, done, progress, resliced, infeasible, rejected —
**no `KICKOUT`** although `stage_feasibility` compares `verdict == "kickout"` and `_outcome` accepts
it. `CheckpointStage` = spec, feasibility, slicing, slices + `CHECKPOINT_ORDER`; `holistic`
deliberately absent (⇒ F8). `Stage` vs reality (F9): `IMPLEMENT="implement"` vs used
`"slice_implement"`; `SLICE_FIT="slice_fit"` vs used `"slice_check"`; `HOLISTIC="holistic_review"`
vs used `"holistic"`; `FIX_TECH`/`FIX_FUNC` declared but the code emits one `"slice_fix"`; no
`spec_assess_tw` / `spec_assess_ornith`; `AUTONOMOUS_*` used only by `autonomous.py`.
**T28 guidance: correct the enum to observed reality — the single source of truth is the `stage`
values in `/home/donald/work/stats/sessions.jsonl` (56 rows). Do not "fix" call-site strings, that
would silently rewrite history and break the stats report.**

**`external/git_cli.py`** (108) — `LAST_GOOD_TAG="pi/last-good"`; `_git(cwd,*args,check=True)`
raises `RuntimeError` on rc≠0; `_has()` probes `refs/heads/` (F6a). `ensure_branch(workdir,task_id,
trunk)`: **`git init -b trunk` + "harness: initial commit" if no `.git`** (F7 — this created a
scratch repo inside `queue/active/002…/`), `git branch trunk` if missing, checkout or `checkout -b
pi/<id> trunk`. `merge_to_trunk(workdir,task_id,trunk,title)`: checkout trunk → `merge --squash`
(check=True, **no abort** ⇒ mid-merge wreck, F6b) → commit → `verify_harness` → on fail
`_revert_to_last_good` + raise → on pass `tag -f pi/last-good trunk` + `branch -d pi/<id>`
(**deletes the branch F8 needs**). `verify_harness(workdir)` runs
`python -c "import harness, harness.workflow.pipeline, harness.workflow.autonomous,
harness.core.session, harness.core.providers, harness.core.gitops, harness.core.stats"` **and**
`python harness.py status` with `cwd=workdir` — harness-repo-specific, so for any other repo the
gate can never pass ⇒ every merge reverted and the task parked (F7). `_revert_to_last_good`:
tag-if-`_has` else `reset --hard HEAD~1` (F6a) — and the `HEAD~1` path is what actually runs today.

**`core/providers.py`** — `Task(id, body, source, meta)`; ABC `TaskProvider` (`fetch_pending()`,
`submit()`); `DirectoryTaskProvider(pending_dir, claimed_dir=None)` (default `pending_dir.parent/"claimed"`,
mkdirs both); `fetch_pending(claim=False)` moves **every** `pending/*.md` → `claimed/` and returns
Tasks; `release_claim(task)` unlinks the staging file by slug match; `submit` no-op;
`_slug = [^a-zA-Z0-9-]+ → "_"`, `[:60]`, else `"task"`; `create_provider(cfg)` supports
`"directory"` only (`pendingDir`, `claimedDir`). **Note the id↔filename mismatch:** file
`003-keep-rejected-features…md` → task id `003_keep_rejected…`; any claim-matching code must slug
both sides.

**`workflow/autonomous.py`** — `AutonomousGenerator(cfg, runner, provider, log=print).run(workdir)`;
`_pending_count()` calls `provider.fetch_pending()` — safe only because `claim` defaults False
(F14: a default flip silently corrupts the loop).

**Live environment.** Work dir `/home/donald/work/{queue,logs,stats,sessions}`. Queue: pending **0**;
active **1** = `002-pipeline-checkpoint-and-resume` whose `task.json` is **old format** (no
`checkpointed_stages`, no `last_updated`, `stage: spec`) while `artifacts/` already hold `spec.md`,
`slices.md` and a feasibility kickback — **and it contains a scratch `.git/`** with one "harness:
initial commit"; parked **2** incl. `001-interrupt-handling` (`task.json` wrongly `"status": "active"`,
and it asks for a stand-down/interrupt command that does not exist); claimed **7** =
`003-keep-rejected-features-for-postereity`, `004-model-refresh`,
`005-stats-for-rejected-auto-ideas-not-showing`, `007-sandbox-for-harness-with-appropriate-firewall-configuration`,
`008-coding-standards`, `auto-3-i-now-have-a-complete-picture-let-me-verify-a-coup`,
`auto-4-now-i-have-a-thorough-understanding-of-the-codebas` (the `auto-*` bodies read as truncated
model monologue, not requirements — flag in D4); done **0**; failed **0**.
Logs: `supervisor.log` **179 MB** (~6 M lines, unrotated), `other/` with stray `.pi-session-*.out`
(several 0 bytes), `autonomous-proposal-{1..4}.md`. Stats: 56 real rows in
`/home/donald/work/stats/sessions.jsonl`. No supervisor/harness process running; no STOP file.
**No CI, no linter config, no `requirements.txt`/`pyproject.toml`, no pytest config; tests are plain
`unittest` via discover.** `docs` for pi itself live outside this repo — irrelevant here.

## 6. Card-specific guidance for T13–T41 (write these, then you're done)

Each bullet = the core of one card. Expand into the template; the finding text in
`AUDIT-2026-08-26.md` §4 supplies the rest.

- **T13 cycle decision** — pure `decide_cycle_action(pending, in_flight, claims) -> str`
  (`"resume"|"work"|"generate"`) in a new import-side-effect-free module (e.g.
  `harness/workflow/cycle.py`). Precedence in-flight > claims (stale ⇒ requeue-then-work) >
  pending > generate. **Say in the card why it must not live in `supervisor.py`: that module runs
  `load(CONFIG_PATH)` at import time, so importing it in a test reads real config and creates dirs.**
- **T14 supervisor → `run-task-loop --continue`** — replace the `if pending: run-one / else:
  autonomous` block with the T13 decision; log `pending=/in_flight=/claimed=`. `[tag]`.
- **T15 no-progress backoff** — see the truthful framing in §5 (`_sleep` already exists).
- **T16 docstring truth** — after T14 the claim becomes true; rewrite to state the contract and
  name the test that proves it (T38). Also fix `harness.py`'s module docstring usage list.
- **T17 stderr drain** — drain thread; add `stderr: str` to `PiSessionResult`; **stop appending
  `[stderr]` into `output`** (that is what lets stderr yield a verdict). `session.py` already reads
  `result.err` for stats `notes` — keep that.
- **T18 wall-clock watchdog** — thread waiting `min(1, deadline-now)` then `proc.kill()`; stdout
  loop must exit and reap. Verify with a fake `pi` script on a temp `PATH` (`sleep 999`) and
  `HARD_TIMEOUT_S` monkeypatched to ~2 s.
- **T19 verdict parse** — `re.IGNORECASE`, `.lower()` the group (enum values are lowercase), and
  parse **assistant text only**, never the stderr block. Table: `VERDICT: DONE`, `verdict: Pass`,
  `VERDICT: kick_out`?, stderr containing `VERDICT: pass` while assistant text has none.
- **T20 unknown vs crash** — crash ⇒ `"error"`; clean-but-silent ⇒ `"no_verdict"`; document that
  `unknown` today drives retry→park loops (F5 last bullet); `_outcome` must keep mapping the 56
  historical rows' verdicts unchanged.
- **T21 status on terminal moves** — `park/fail/complete` write `TaskStatus.*.value` **after** the
  move (new path) and before `_exec_summary`; tolerate a missing `task.json` (write a minimal one,
  don't raise). Live bug to cite: `queue/parked/001-interrupt-handling/task.json` says `"active"`.
- **T22 record workdir** — add `workdir: str = ""` to `TaskState` + `to_json`; `intake` records the
  resolved workdir; `process()` prefers the recorded value, falls back to `resolve_workdir` only
  when empty; `load_state` defaults it (old-format `task.json` must still load); add `harness.py`
  output check that a resumed task logs the same workdir twice.
- **T23 no `git init` in the queue** — `ensure_branch` (or a guard above it) refuses any workdir
  under `cfg.queue_dir`; park with a clear reason instead. Cite the live scratch repo
  `queue/active/002-…/.git`. Decide + state: guard belongs in `pipeline.process` (it knows
  `queue_dir`) calling a `git_cli` predicate, or `ensure_branch` takes `deny_prefix`. `[tag]`.
- **T24 repo-aware gate** — config-declared `verifyCommands` per repo (list of argv lists, run with
  `cwd=workdir`), and **no declared gate ⇒ refuse to merge and park** (D3 recommendation: an
  undeclared gate is a silent pass). Keep `verify_harness` working for the harness repo itself.
  Out of scope: running real test suites in the card's verify block — use a temp repo + `echo ok`.
- **T25 queue surgery** — operator card, needs D4: `requeue-claims` the real 7, normalize `002`
  `task.json` (backfill `checkpointed_stages` from `artifacts/` presence, decide with the human),
  remove the scratch `.git` from `queue/active/002…`, clear stray `.pi-session-*.out`, archive or
  delete `auto-3`/`auto-4`. Every step: print plan, require `--yes`, back up to
  `queue/_pre-T25-<date>/`. **This card must be written so the agent never guesses.**
- **T26 per-slice checkpoint** — `TaskState.checkpointed_slices: list[str]`; `stage_slices` skips
  completed slice ids and appends after a slice passes all reviews; a crash at slice 3/5 resumes at 4.
  Keep `CheckpointStage.SLICES` as the stage-level marker (do not overload it). Order + dedupe rules
  mirror `_parse_stages`.
- **T27 merge checkpoint** — add `CheckpointStage.MERGE` (or a `merged: bool`) recorded after a
  successful `merge_to_trunk` and before `complete()`; **stop deleting the feature branch in
  `merge_to_trunk`** — delete it in `complete()` instead (F8: today a crash between squash-merge and
  `complete()` resumes into `stage_holistic` where `merge --squash` now fails because the branch is
  gone ⇒ spurious park). `[tag]`
- **T28 enum values** — rewrite `Verdict` (+`KICKOUT`, `UNKNOWN`, `ERROR`, `NO_VERDICT` if T20 lands)
  and `Stage` from the actual `stage`/`verdict` values in `stats/sessions.jsonl`; keep the wire
  strings identical so the report is unchanged. Depends: T20.
- **T29 verdict at call sites** — mechanical replacement of the 12 comparisons in `pipeline.py` with
  `Verdict.*` (compare enum-to-enum; `.value` only where a string must cross into stats/logs).
  Verify must include a diff-audit grep proving no `verdict == "` remains.
- **T30 stage at call sites** — replace the raw stage strings (§5 lists all 11, incl. the f-string
  `f"{kind}_review"`) with `Stage` members; stats rows for a synthetic run must be byte-identical to
  before (assert the exact `stage` value in a test).
- **T31 dead-code sweep** — unused imports (`ensure_branch` task_lifecycle l.19; `shutil`+
  `CheckpointStage` resume l.9/13; `build`+`CONFIG_PATH` harness.py l.20/25; partial `re`/`shutil`
  in pipeline), `_plan_stages` enum/string mix, `_log` dedupe if T07 did not take it.
  Verify: `python3 -m compileall` + Gate + a grep table in the commit message.
- **T32 window vs budget** — needs D2. Recommendation: `modelContext` = real window (restore
  65536/131072), new `maxPromptTokens` = working cap, `model_budget() = min(cap, window - reserve)`,
  and rename `model_context()` → keep name, fix truthfulness. Must not change any prompt text for
  the models actually in use unless D2 says so.
- **T33 log units + config keys** — fix `budget={n}k` (print real `k` with one decimal, or drop the
  suffix), add `maxCrashRetries` to `config.json` explicitly, trailing newline, and the README
  command reference + budget section (README currently references deleted `./supervisor.sh` and
  `harness/providers.py`, omits `claimed/`, documents no `resume`/`--continue`/`--fresh`/`run-one`/
  `run-task-loop`, and claims `work/logs/harness.log` is written — true only after T07). Also
  re-sync `REFACTOR_PLAN.md` + `refactor-tasks/README.md` ("Not started until Task 7" is stale —
  automation has already run) and task 002 slice 4's EC12 doc. Consider splitting README vs
  refactor-docs if the card exceeds 65 lines.
- **T34–T40 test cards** — each: `unittest` only (no pytest dep), fixtures in `tmp_path`-style temp
  dirs, a fake `pi` shell script on `PATH` for subprocess tests, temp git repos for `git_cli`, and
  **no network, no real queue, no real supervisor**. T37 handlers: monkeypatch `H.build` with a
  stub provider + stub pipeline (session-1 proved this pattern works — see `T10`/`T12` verify
  blocks). T38 supervisor: import the T13 pure module, never `supervisor.py`. T39 stats: fixture
  JSONL, assert `render_report` numbers. T40 deps+CI: `pyproject.toml` (+ dev extras), ruff config
  **as an advisory first pass** (do not enable rules that fail on the existing tree — list the
  residual count instead), CI job = unittest + Gate; **D5 first** (there is no evidence of a remote
  — if none, ship `scripts/gate.sh` + documented pre-push hook instead of Actions).
- **T41 small items** — group of 5 truly small fixes, each with its own verify line: `resume --fresh`
  (reuse `fresh_restart`); func-review fix session on the **implementer** model (and state *why* it
  was wrong); split the progress-note path so review feedback and implementer notes don't overwrite
  (`artifacts/progress/slice-{sid}.md` vs `…-review.md`); `_run` signalling when all attempts crashed
  (add a field or raise — pick one and update callers); `AutonomousGenerator._pending_count` pinned
  to a read-only fetch (`limit=0`-style or a new `count_pending()`) + test that a claim-default flip
  cannot happen. If this card grows past 65 lines, split into T41/T42 and update the index.

## 7. Open decisions (mirror of PLAN §"Open decisions" — do not silently resolve)

- **D1** (blocks T01) uncommitted `config.json`/`README.md` + deleted `supervisor.sh`: commit or discard.
- **D2** (blocks T32) is `tokenBudget` a cap or window truth; and is a 60k cap on 128k models
  deliberate (throughput) or an oversight.
- **D3** (blocks T24) verification gate for non-harness repos: config-declared commands vs detect vs
  refuse. Recommendation: refuse-and-park when undeclared.
- **D4** (blocks T25) requeue all 7 claims or cull; `auto-3`/`auto-4` look like non-requirements.
- **D5** (blocks T40) is CI wanted, on what runner.
- **D6** (not blocking, deliberately out of plan) parked `001` wants a stand-down/interrupt command.

Write cards so a card blocked on a decision is still *writable*: put the decision in **Context**, the
recommended default in **Do**, and "if the human's answer differs, STOP and hand over" in **Done when**.

## 8. Progress log (update this as you land each pass)

- [x] Session 1 — `PLAN-2026-08-26.md` index (waves, deps, decisions, gate, rules).
- [x] Session 1 — cards `T01`–`T12` (waves 0–2) written.
- [x] Session 1 — this handover.
- [x] Session 2 — cards `T13`–`T17` written (session 2 ended abnormally at this point; see
      `HANDOFF-CONTINUATION.md`, which is **not** a reliable account of it).
- [x] Session 3 — cards `T18`–`T42` written (`T42` added, see below).
- [x] Session 3 — `PLAN-2026-08-26.md` index updated: 42 cards, three index lines renamed
      (`T24-refuse-merge-without-gate.md`, `T25-queue-audit-readonly.md`,
      `T40-pyproject-and-gate-script.md`), `T32 ──> T42` added to the dependency graph.
- [x] Session 3 — **all 42 card files exist. Card writing is complete.** No source file was read for
      editing or modified; nothing was executed.

### Deviations from §6, all caused by the human answers now recorded in `PLAN-2026-08-26.md`

§6 was written before D2–D5 were answered. Where they disagree, **the answers in the PLAN file win**;
the cards were written to the answers, not to §6.

- **T24** — §6 says "config-declared `verifyCommands` per repo". **D3 deferred the per-repo gate
  design.** T24 now refuses to merge (before any git write) when the harness gate does not apply, and
  adds no config key and no toolchain detection.
- **T25** — §6 says "queue surgery … requeue the real 7, normalize 002, remove the scratch `.git`".
  **D4 says leave everything where it is.** T25 is now `queue-audit`: read-only inventory + anomaly
  report + operator suggestions, with an explicit "if you reach for `shutil`, stop" rule.
- **T40** — §6 says "CI job = unittest + Gate … or `scripts/gate.sh` + pre-push hook". **D5: no
  remote, no CI.** T40 ships `pyproject.toml` + advisory ruff + `scripts/gate.sh`; no `.github/`, no
  hook (a pre-push hook cannot fire without a remote).
- **T42 (new)** — **D2** added a hard requirement with no owning card: *"the second context usage goes
  over 60k tokens I want an immediate park and handoff for next agent via markdown, no questions
  asked."* T42 enforces it on `peak_tokens` > `maxPromptTokens` (60000): no retry, park, and extend
  `queue/review/<id>.md` with `## Handoff` / `## Next agent should`. T32 supplies the cap.
- **T28** — §6 said the ground truth is the `stage` values in `sessions.jsonl`; those values are now
  written into the card itself (verified read-only 2026-08-27): `autonomous_suggest` 12,
  `spec_author` 10, `slice_implement` 6, `spec_assess_tw` 4, `spec_assess_ornith` 4, `slice_check` 4,
  `autonomous_review` 4, `feasibility` 3, `tech_review` 2, `smoke` 2, `slicing` 2, `func_review` 2,
  `smoke32k` 1. Verdicts: `unknown` 21, `pass` 15, `done` 14, `error` 3, **`reject` 2** (not
  `rejected` — the enum is wrong on the wire value), `kickback` 1. Two additions to §6's list:
  `smoke`/`smoke32k` are ad-hoc manual runs and must **not** become enum members, and `holistic` /
  `slice_fix` exist in code but have **no** historical rows.

### Remaining bookkeeping (deliberately not done)

- This file is kept rather than deleted: it is the only place the verified code intelligence in §5 and
  the §6→card reconciliation are written down. Delete it once the cards have been *executed*, not now.
- `T13`–`T42` are untracked. Committing is an action, so it was not taken here; the operator's call.
- Nothing in `/home/donald/work/queue`, `/home/donald/work/stats` or `/home/donald/work/logs` was
  modified.

## 9. Sanity checks available to you (cheap, read-only)

```bash
cd /home/donald/work/harness
ls plan-2026-08-26/ | sort          # T01..T12 exist today
python3 -m unittest discover -s tests        # 40 passed — unchanged, you touch no code
git status --short                  # unchanged from §2
python3 harness.py status           # shows the six rows (claimed row appears after T11 lands)
python3 -c "import json,collections;rows=[json.loads(l) for l in open('/home/donald/work/stats/sessions.jsonl')];print(len(rows));print(collections.Counter(r['stage'] for r in rows).most_common());print(collections.Counter(r['verdict'] for r in rows).most_common())"   # ground truth for T28/T30
```

If any of those disagree with §2/§5, trust the command, note it here, and continue.
