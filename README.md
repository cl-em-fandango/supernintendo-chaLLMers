# Autonomous Workflow Harness

A self-driving pipeline that turns freeform task descriptions into reviewed,
merged features — using fresh, token-budgeted pi sessions at every step.

## The pipeline (per task)

```
pending/  ──▶  SPECIFICATION
                1a. TechnicalWriter  : author functional spec
                1b. Ornith (assessor): assess + amend  ─┐ kickback (max 3)
                1c. TechnicalWriter  : check vs. original ─┘
              ──▶  FEASIBILITY  (Implementer)
                    pass | kickback→spec | kickout→failed/
              ──▶  SLICING  (Implementer)
                    vertical atomic slices, each ≤ 1 session at the model's
                    working budget + recursive fit-check (fresh session per check)
              ──▶  PER-SLICE (on branch pi/<task>)
                    implement (≤5 iters, progress notes between sessions)
                    → tech review (≤5, Implementer)
                    → func review (≤5, TechnicalWriter)
                    → commit
              ──▶  HOLISTIC REVIEW  (TechnicalWriter)
                    pass → squash-merge to pi/trunk → done/
                    fail → parked/ (branch kept, not merged)
```

Every stage is checkpointed (`spec`, `feasibility`, `slicing`, `slices`, plus a
`merge` marker) in `active/<task>/task.json`, so `resume` restarts from the last
completed stage rather than from the beginning.

When the queue is empty → **autonomous mode**: random models from
`models.fastPool` (falling back to `models.randomPool`) propose features, a
second random model vetoes the dumb ones, survivors land in `pending/` until the
queue has `autonomousQueueTarget` tasks.

## Design principles

- **Fresh context every session.** No session resume. Continuity lives in
  explicit artifacts (spec.md, slices.md, progress notes), never in model memory.
  Pi's own session store is disabled (`pi --no-session`).
- **Working cap vs. real window.** `maxPromptTokens` is the prompt cap; a session
  that crosses it is parked and handed off. `modelContext` maps each model to its
  real window, and the per-model budget is
  `max(4096, min(maxPromptTokens, window - 8192))` — the 8192 is output headroom.
- **Verdict protocol.** Every session ends with `VERDICT: <value>`; the harness
  routes on it. No NLP guessing.
- **Adapter pattern** for task sources (`harness/core/providers.py`). The
  pipeline only talks to the `TaskProvider` interface — swap in GitHub, an API, a
  DB without touching the pipeline.
- **Unified stats store.** Every session is one JSONL row in
  `work/stats/sessions.jsonl` with model, stage, verdict, tokens, duration.
  `harness.py report` answers: which model gets rejected most, which is
  consistently poor, where work bounces, speed and token cost per stage/task.

## Layout

```
work/
  harness/            this repo (the engine)
  queue/
    pending/          drop .md task files here
    claimed/          a run's in-flight claims (sidecar names the owning run)
    active/           task currently in flight (state + artifacts)
    review/           one exec-summary .md per finished task
    done/  failed/  parked/
  stats/sessions.jsonl
  logs/
    harness.log       every harness log line (+ harness.log.1 after rotation)
    supervisor.log    supervisor loop (+ supervisor.log.1 after rotation)
    supervisor.pid    single-instance lockfile
    children/         <UTC ts>-<subcommand>.log per supervised child (stdout+stderr)
  STOP                touch this to halt the supervisor gracefully
```

`work/queue/*`, `work/logs/` and `work/stats/sessions.jsonl` are created by the
composition root (`harness/composition.py`) on every command.

## Usage

Subcommands and flags are defined in `harness/cli/parser.py`; each one answers
`python3 harness.py <cmd> --help`.

```bash
cd work/harness
python3 harness.py run                 # all pending, one claim at a time, then autonomous
                                       # --continue, --requeue-stale
python3 harness.py run-task <file.md>  # one task  --continue, --fresh
python3 harness.py run-one             # claim and process exactly one pending task
python3 harness.py run-task-loop       # pending until empty, then exit
                                       # --continue, --requeue-stale
python3 harness.py autonomous          # just generate tasks
python3 harness.py status              # queue (all seven dirs) + stats
python3 harness.py report              # stats only
python3 harness.py resume <task_id>    # resume from the last checkpoint
                                       # --yes/-y, --fresh
python3 harness.py unpark <task_id>    # parked/ or failed/ → pending/ (alias: requeue)
python3 harness.py requeue-claims      # hand stranded claimed/ files back to pending
                                       # --older-than HOURS, --dry-run
```

- `--continue` resumes in-flight tasks in `active/` before the pending queue is
  worked; `--fresh` deletes an existing `active/` dir and restarts that task from
  scratch (`run-task`, `resume`).
- `--requeue-stale` (`run`, `run-task-loop`) reclaims this invocation's own claims
  older than `CLAIM_STALE_HOURS` at startup. Off unless flagged, or
  `"autoRequeueStaleClaims": true` in `config.json`.
- `requeue-claims` is the operator command for claims a dead run left behind
  (`--dry-run` to preview, `--older-than HOURS` to bound it). A claim with no
  readable owner sidecar is refused, not moved.

## Supervisor

`supervisor.py` (pure Python) keeps the harness
running in bounded cycles. It decides each cycle from `pending/`, `active/` and
`claimed/`, spawns one child (`run-task-loop --continue`, or `autonomous` when
there is nothing to work), and backs off when a cycle changes no task identity.

```bash
cd work/harness
python3 supervisor.py start     # daemonize and run
python3 supervisor.py status    # is it running?
python3 supervisor.py stop      # SIGTERM the supervisor and its child tree
python3 supervisor.py run       # the loop in the foreground
touch ../STOP                   # alternative graceful stop
```

Unknown or missing arguments print the module docstring; `supervisor.py` has no
`--help` flag.

## Configuration (`config.json`)

| key | default | meaning |
|-----|---------|---------|
| `workDir` | — | root of `queue/`, `stats/`, `logs/` |
| `maxPromptTokens` | 60000 | prompt cap; crossing it parks the task |
| `tokenBudget` | 60000 | legacy spelling of `maxPromptTokens` (fallback only) |
| `maxSpecKickbacks` | 3 | spec kickback rounds before the stage fails |
| `maxSliceImplement` | 5 | implement iterations per slice |
| `maxSliceTechReview` | 5 | tech-review iterations per slice |
| `maxSliceFuncReview` | 5 | func-review iterations per slice |
| `maxSliceCheckLoops` | 3 | recursive slice fit-check loops |
| `maxCrashRetries` | 2 | retries after a crashed/timed-out session before parking |
| `autonomousQueueTarget` | 5 | pending depth autonomous mode fills to |
| `trunkBranch` | `pi/trunk` | merge target; also the branch the breaker rolls back |
| `modelContext` | {} | model → real context window (tokens); an unmapped name whose suffix says 32k/64k/128k uses that, anything else falls back to 131072 |
| `taskProvider` | `directory` | which `TaskProvider` to build |
| `directoryProvider.pendingDir` | `<workDir>/queue/pending` | where task files are read from |
| `models.technicalWriter` / `implementer` / `assessor` | — | role → model for the pipeline stages |
| `models.fastPool` | — | high-volume, low-stakes stages (autonomous suggest/review) |
| `models.randomPool` | — | full proposal pool; also the fastPool fallback |
| `autoRequeueStaleClaims` | false | opt-in startup stale-claim reclaim for `run`/`run-task-loop` |

Environment overrides: `HARNESS_CONFIG` (config path), `CLAIM_STALE_HOURS`
(6.0), `HARNESS_PI_PROVIDER` (`llama-swap`), and for the supervisor `SLEEP_S`
(60), `SUPERVISOR_MAX_SLEEP_S` (900), `MAX_CYCLES` (0 = unlimited),
`FAIL_LIMIT` (3), `SUPERVISOR_MAX_LOG_BYTES` (5000000),
`SUPERVISOR_MAX_CHILD_LOGS` (50).

## Models (llama-swap)

| role              | model                          |
|-------------------|--------------------------------|
| technical writer  | Qwen3.8-DFLASH2-TechnicalWriter|
| implementer       | Qwen3.8-DFLASH2-Implementer    |
| assessor          | Ornith-1.5-35B-Q6_K.gguf       |
| autonomous pool   | `models.fastPool` (7 models), falling back to `models.randomPool` (12) |

## Adding a task provider

Subclass `TaskProvider` in `harness/core/providers.py`, implement
`fetch_pending()` (and optionally `submit()`), and register it in
`create_provider()` in the same file. Set `taskProvider` in `config.json` to its
name.
