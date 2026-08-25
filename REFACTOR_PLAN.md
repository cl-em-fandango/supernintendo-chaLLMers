# Refactor Plan: harness → CODING_STANDARDS

Goal: restructure the existing harness so it follows CODING_STANDARDS.md, in
small, independently-verifiable chunks. No automation runs until this is done.
Each chunk ends with the verification gate passing (`import harness` +
`harness.py status`) and a commit.

## Current state (ground truth)

```
harness.py            209  CLI dispatch + composition root (mixed)
harness/
  __init__.py          13
  config.py            90  Config dataclass + load()          [OK: leaf]
  stats.py            186  SessionRecord + StatsStore + report [OK: leaf]
  providers.py        108  Task dataclass + TaskProvider ABC   [OK: leaf]
  prompts.py          251  prompt builders                    [OK: leaf]
  session.py          202  SessionRunner (shells out to pi)    [VIOLATION: shells out directly]
  gitops.py           111  git helpers (shells out to git)     [VIOLATION: shells out directly]
  pipeline.py         354  Pipeline: intake/park/fail/complete + 5 stages + helpers [GRAB-BAG]
  autonomous.py       99   AutonomousGenerator                [OK: workflow]
```

Dependency direction today is roughly flat (everything imports from the
package root). The standards want:
`cli → workflow → (session/stats/providers/prompts) → external → nothing`.

## Violations to fix

1. **Subprocess calls not isolated in `external/`.** `session.py` and
   `gitops.py` both call `subprocess` directly. Standards: all subprocess
   behind `external/pi_cli.py` and `external/git_cli.py`.
2. **`pipeline.py` is a grab-bag.** It mixes task-lifecycle state transitions
   (intake/park/fail/complete/exec-summary), the stage orchestration, and
   parsing helpers. Standards: one responsibility per file.
3. **`harness.py` mixes CLI and composition.** It has argparse-free manual
   dispatch, command handlers, AND the `build()` composition root. Standards:
   `cli/` only parses/dispatches; the composition root is separate and thin.
4. **Magic strings for state.** Task status (`"active"`, `"parked"`,
   `"done"`, `"failed"`) and verdicts (`"pass"`, `"fail"`, `"kickback"`,
   `"done"`, `"progress"`, ...) are bare strings. Standards: enums.
5. **No explicit parameters objects.** `Pipeline.__init__` takes
   `(cfg, runner, log, provider)` — acceptable, but stage methods take
   `(tid, td, workdir, ...)` positional tuples of paths. Standards: named
   state, not positional path tuples.

## Target structure

```
harness.py                 thin composition root: build() + dispatch to cli/
cli/
  __init__.py
  parser.py                argparse parser (replaces manual sys.argv dispatch)
  handlers.py              cmd_* functions, one per subcommand
external/
  __init__.py
  pi_cli.py                spawn pi, stream JSON, return SessionResult
  git_cli.py               branch/merge/verify/revert/tag operations
workflow/
  __init__.py
  pipeline.py              stage orchestration only (spec→feasibility→slicing→slices→holistic)
  task_lifecycle.py        intake/park/fail/complete/exec-summary (state transitions)
  autonomous.py            AutonomousGenerator (moved here)
  params.py                PipelineParams, StageContext (named state objects)
core/
  __init__.py
  config.py                (moved, unchanged)
  stats.py                 (moved, unchanged)
  providers.py             (moved, + TaskStatus enum)
  prompts.py               (moved, + Verdict enum)
  session.py               SessionRunner (now calls external/pi_cli, not subprocess)
  enums.py                 TaskStatus, Verdict, Stage (shared enums)
```

Dependency direction: `cli → workflow → core → external → nothing`.
`external` and `core` never import from `workflow` or `cli`.

## Chunks (each = one commit, gate must pass)

Order is chosen so every intermediate state is importable and the gate passes.
Moves come before behavior changes so each step is small and reviewable.

### Chunk 1 — enums (additive, zero risk)
- Add `core/enums.py` with `TaskStatus`, `Verdict`, `Stage` enums.
- No call sites changed yet (purely additive).
- Gate: import + status.

### Chunk 2 — create `external/`, move subprocess out of `session.py`
- Create `external/pi_cli.py`: the raw subprocess + JSON-streaming logic from
  `session.py` (the `_run`/heartbeat/parse internals), returning a
  `SessionResult`.
- `core/session.py` `SessionRunner` becomes a thin wrapper that calls
  `external/pi_cli.run_session(...)`.
- Gate: import + status. (No live session run yet — that's chunk 7.)

### Chunk 3 — move git subprocess into `external/git_cli.py`
- Create `external/git_cli.py` with the `_git`/`_has`/branch/merge/verify/
  revert/tag functions from `gitops.py`.
- `core/gitops.py` (or fold into external) becomes a thin wrapper.
- Gate: import + status.

### Chunk 4 — relocate core modules into `core/`
- Move `config.py`, `stats.py`, `providers.py`, `prompts.py`, `session.py`,
  `gitops.py` → `core/`. Update all imports.
- Pure move; no logic change.
- Gate: import + status.

### Chunk 5 — split `pipeline.py` into `workflow/`
- Create `workflow/task_lifecycle.py`: intake/park/fail/complete/_exec_summary
  + the TaskStatus enum usage.
- Create `workflow/params.py`: `StageContext` (tid, td, workdir) named object
  replacing the positional `(tid, td, workdir)` tuples.
- `workflow/pipeline.py`: stage orchestration only, using `StageContext` and
  `task_lifecycle`.
- Move `autonomous.py` → `workflow/`.
- Gate: import + status.

### Chunk 6 — split `harness.py` into `cli/` + thin root
- Create `cli/parser.py` (argparse) and `cli/handlers.py` (cmd_* functions).
- `harness.py` becomes: `build()` composition root + `main()` that calls the
  parser. No business logic.
- Gate: import + status + a real `harness.py status` and `harness.py report`.

### Chunk 7 — end-to-end verification (no code change, just proof)
- Run a real task through the pipeline (or a smoke session) to confirm the
  refactor didn't break runtime behavior, not just imports.
- Confirm: a session runs, verdicts parse, stats record, git branch works.
- This is the gate before automation is allowed to run.

## Out of scope (separate tasks)
- The `007-sandbox` worktree isolation (different concern).
- Adding new features.
- Changing model routing or budgets.

## Rollback
Every chunk is a commit on `pi/trunk` with `pi/last-good` advanced only after
the gate passes. If a chunk breaks, `git reset --hard pi/last-good` restores
the last known-good state. The supervisor circuit breaker would do this
automatically, but we're not running it yet.
