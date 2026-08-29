# Refactor Plan: harness → CODING_STANDARDS

> **STATUS: LANDED.** All seven chunks are on `pi/trunk` and the tree follows
> `CODING_STANDARDS.md`. Automation now runs: `supervisor.py` drives the loop in
> bounded cycles, with its own circuit breaker (see *Rollback* below). Sections
> that described the pre-refactor tree have been replaced with the landed one; the
> original snapshot is in git history.

Goal: restructure the existing harness so it follows CODING_STANDARDS.md, in
small, independently-verifiable chunks. Each chunk ended with the verification
gate passing (`import harness` + `harness.py status`) and a commit.

## Landed structure (ground truth)

```
harness.py                 CLI entry point: parse args, dispatch to harness/cli/handlers
supervisor.py              bounded-cycle loop, single instance, backoff, circuit breaker
harness/
  composition.py           composition root: build() wires cfg/stats/session/provider/pipeline
  cli/
    parser.py              argparse parser (replaced the manual sys.argv dispatch)
    handlers.py            cmd_* functions, one per subcommand, no business logic
  core/
    enums.py               TaskStatus, Verdict, Stage, CheckpointStage, ReviewKind
    config.py              Config dataclass + load() + model window/budget maths
    stats.py               SessionRecord + StatsStore + render_report
    providers.py           Task + TaskProvider ABC + DirectoryTaskProvider + create_provider()
    prompts.py             prompt builders
    session.py             SessionRunner (calls external/pi_cli, not subprocess)
    gitops.py              thin re-export of external/git_cli
    logsink.py             LogSink: stdout + work/logs/harness.log, rotating at 5 MB
    claim_metadata.py      the claimed/ ownership sidecar
    enqueue_guard.py       refuse-to-enqueue checks on generated tasks
  workflow/
    pipeline.py            stage orchestration (spec→feasibility→slicing→slices→holistic)
    task_lifecycle.py      intake / park / fail / complete / exec-summary
    autonomous.py          AutonomousGenerator
    continue_fresh.py      --continue resume-in-flight and --fresh restart
    resume.py              resume from checkpoint
    spec_assessment.py     assessor verdict routing (fails closed)
    cycle.py               pure supervisor cycle decision + child command mapping
    params.py              StageContext (named state, replaces positional path tuples)
external/
  pi_cli.py                spawn pi, stream JSON, return SessionResult
  git_cli.py               branch/merge/verify/revert/tag operations
```

Dependency direction: `cli → workflow → core → external → nothing`.
`external` and `core` never import from `workflow` or `cli`.

## Violations — all closed

1. **Subprocess calls not isolated in `external/`.** Fixed: `external/pi_cli.py`
   owns the pi subprocess and JSON streaming; `external/git_cli.py` owns git.
   `harness/core/session.py` and `harness/core/gitops.py` are thin wrappers over
   them.
2. **The monolithic pipeline module was a grab-bag.** Fixed: state transitions
   moved to `harness/workflow/task_lifecycle.py`; `harness/workflow/pipeline.py`
   orchestrates stages only.
3. **`harness.py` mixed CLI and composition.** Fixed: `harness/cli/parser.py`
   parses, `harness/cli/handlers.py` dispatches, `harness/composition.py` is the
   composition root. `harness.py` wires the two together.
4. **Magic strings for state.** Fixed: `harness/core/enums.py` holds `TaskStatus`,
   `Verdict`, `Stage`, `CheckpointStage`, `ReviewKind`. Strings survive only at
   the edges (the `VERDICT:` line a model emits, git ref names).
5. **No explicit parameters objects.** Fixed: stage methods take a `StageContext`
   (`harness/workflow/params.py`); `Pipeline.__init__` takes
   `(cfg, runner, log, provider)`; the log sink is passed explicitly from the
   composition root rather than held in a module global.

## Verification gate

Every change still ends with:

```bash
cd work/harness
python3 -c "import harness"
python3 harness.py status
```

## Context budget (aim for 25k max before compacting)
- **Target: keep the working context at 25k tokens or fewer before compacting**, so
  we can do the compacting ourselves (rather than being forced to compact at a
  higher, less-controlled threshold).
- During the refactor the context reached **32k** — over the 25k target. Treat 25k
  as the soft ceiling: when the running context approaches it, compact proactively.
- This applies to every chunk/supervisor turn, not just the refactor.

## Out of scope (separate tasks)
- The `007-sandbox` worktree isolation (different concern).
- Adding new features.
- Changing model routing or budgets.

## Rollback

`pi/last-good` is a **tag** (`refs/tags/pi/last-good`), advanced only
after a chunk's gate passes. The revert is not a manual command: the supervisor's
circuit breaker calls `external.git_cli.revert_to_last_good(workdir, trunk)` after
`FAIL_LIMIT` consecutive launch failures, and that call refuses to run in a
worktree with uncommitted changes — the refusal is logged and the loop continues.
`AGENTS.MD` forbids a raw `git reset --hard` from an agent session.
