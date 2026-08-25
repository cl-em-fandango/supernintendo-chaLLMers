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
                    vertical atomic slices, each ≤ 1 session @ 128k
                    + recursive fit-check (fresh session per check)
              ──▶  PER-SLICE (on branch pi/<task>)
                    implement (≤5 iters, progress notes between sessions)
                    → tech review (≤5, Implementer)
                    → func review (≤5, TechnicalWriter)
                    → commit
              ──▶  HOLISTIC REVIEW  (TechnicalWriter)
                    pass → squash-merge to pi/trunk → done/
                    fail → parked/ (branch kept, not merged)
```

When the queue is empty → **autonomous mode**: random models propose features,
a second random model vetoes the dumb ones, survivors land in `pending/`
until the queue has 5 tasks.

## Design principles

- **Fresh context every session.** No session resume. Continuity lives in
  explicit artifacts (spec.md, slices.md, progress notes), never in model memory.
- **Token budget** (default 100k) keeps every session safely under the 128k window.
- **Verdict protocol.** Every session ends with `VERDICT: <value>`; the harness
  routes on it. No NLP guessing.
- **Adapter pattern** for task sources (`harness/providers.py`). The pipeline
  only talks to the `TaskProvider` interface — swap in GitHub, an API, a DB
  without touching the pipeline.
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
    active/           task currently in flight (state + artifacts)
    done/  failed/  parked/
    review/           one exec-summary .md per finished task
  stats/sessions.jsonl
  logs/               harness.log, supervisor.log
  sessions/           (unused; --no-session)
```

## Usage

```bash
cd work/harness
python3 harness.py run-task <file.md>   # one task
python3 harness.py run                  # all pending, then autonomous
python3 harness.py autonomous           # just generate tasks
python3 harness.py status               # queue + stats
python3 harness.py report               # stats only

# self-driving loop (bounded cycles, stoppable):
nohup ./supervisor.sh >> ../logs/supervisor.log 2>&1 &
./supervisor.sh status
./supervisor.sh stop          # or: touch work/STOP
```

## Models (llama-swap)

| role              | model                          |
|-------------------|--------------------------------|
| technical writer  | Qwen3.8-DFLASH2-TechnicalWriter|
| implementer       | Qwen3.8-DFLASH2-Implementer    |
| assessor          | Ornith-1.5-35B-Q6_K.gguf       |
| autonomous pool   | all 20 llama-swap models       |

## Adding a task provider

Subclass `TaskProvider` in `harness/providers.py`, implement
`fetch_pending()` (and optionally `submit()`), and register it in
`create_provider()`. Set `taskProvider` in `config.json` to its name.
