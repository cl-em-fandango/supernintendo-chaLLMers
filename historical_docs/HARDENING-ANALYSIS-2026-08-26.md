# Hardening Plan Review — 2026-08-26

Scope: review of `PLAN-2026-08-26.md`, cards `T01`–`T42`, the audit, and the current Python implementation. This document identifies gaps, flawed reasoning, and implementation concerns only; it does not propose unrelated features.

## Executive summary

The plan is unusually disciplined about card boundaries, destructive operations, reproducible verification, and preserving historical wire values. Its highest-risk weaknesses are:

1. **A current fail-open path is not covered:** specification assessment treats any verdict other than `kickback` as approval, including process errors and missing/unknown verdicts.
2. **T42 does not implement the requested “immediate” 60k stop:** it checks `peak_tokens` only after the subprocess has returned, and its proposed stats/handoff data flow is incomplete.
3. **T22 has a circular intake design:** resolving the workdir currently requires `original.md`, but `original.md` is created by `intake()`, while the card asks `intake()` to receive the already-resolved workdir.
4. **The supervisor’s claim policy knowingly enters a blocked state:** claims are classified as actionable work even though the spawned command cannot process them with stale reclaim disabled.
5. Several card verify blocks are source-text checks or contain scaffolding that cannot run as written, reducing confidence that card-level success proves runtime behavior.

## 1. Obvious gaps

### G1 — Specification approval currently fails open on invalid assessor results

**Severity: critical**

In `Pipeline.stage_spec()`:

- Ornith causes a retry only when `r.verdict == "kickback"`.
- The technical-writer check causes a retry only when `r.verdict == "kickback"`.
- Every other value falls through to `spec approved`.

Consequently, `error`, `unknown`, `no_verdict`, `fail`, or any unexpected value can approve a specification. T19/T20/T28/T29 improve parsing and typing but do not correct this routing. In fact, after T20 makes failures more truthful, the assessor path still approves them.

The hardening list should explicitly require positive approval verdicts at both assessment steps and park/retry on process or protocol failures. This is not an additional feature; it is a missing fail-closed correction in the existing verdict hardening.

### G2 — `SessionResult.ok` remains operationally ignored

**Severity: high**

`SessionRunner.run()` exposes `ok`, but pipeline stages route almost entirely on `verdict`. T20 distinguishes crash/no-verdict/unknown, and T41 handles the special case where every retry crashes, but clean nonzero/protocol-invalid combinations and unexpected assessor verdicts remain inconsistently handled.

The plan needs one explicit invariant for stage routing: a session must satisfy both process health and an allowed verdict for that stage. Without that, enum adoption is mostly representational rather than a complete hardening boundary.

### G3 — No permanent behavior test covers assessor fail-closed routing

**Severity: high**

The added tests cover parsing, subprocess behavior, git, handlers, cycle logic, stats, queue guards, merge checkpointing, and over-cap behavior. No card adds a pipeline test asserting that assessor `error`, `unknown`, or `no_verdict` cannot approve a spec. Existing pipeline tests are described as a regression net, but this specific defect is not named or owned.

### G4 — No concurrency/ownership safety for claims

**Severity: medium/high**

T09–T12 improve claim visibility and recovery, but claim identity remains filename/slug based, with no owner/run identifier. `claim_age_hours()` uses mtime and requeue operations rename files without proving which process owns them. The cards explicitly defer locking, yet the supervisor and manual commands can coexist.

At minimum, the plan should identify concurrent invocation as an unresolved hardening risk. Otherwise “stale” is indistinguishable from a legitimately long-running claim, and an operator command can requeue work another process owns.

### G5 — Failure during terminal moves is not handled transactionally

**Severity: medium**

T21 updates `task.json` after `shutil.move()`. If the move succeeds and state write fails, the task is terminal by directory but has stale state; if summary creation fails, pipeline completion can raise after the task has moved. The card says bookkeeping must not raise for missing/corrupt JSON, but does not address I/O failure after the move.

The plan correctly accepts directory location as authority, but should state and test the failure contract for move-success/state-write-failure and move-success/summary-write-failure.

## 2. Obviously flawed or incomplete reasoning

### F1 — T42 is not an immediate over-cap stop

**Severity: critical**

T42 checks `result.peak_tokens` in `Pipeline._run`, which runs only after `SessionRunner.run()` and `run_pi_session()` return. A model can cross 60k and continue consuming context for the remainder of the session. That does not satisfy “the second context usage goes over 60k tokens I want an immediate park and handoff.”

The signal is available while parsing each `message_end`/`agent_end` event in `external/pi_cli.py`; enforcing the limit only after subprocess completion is retrospective detection, not an immediate stop.

### F2 — T42 cannot update the existing stats row as specified

**Severity: high**

`SessionRunner.run()` writes the stats row before returning to `Pipeline._run`. T42 then detects the excess in `Pipeline._run` and says the row’s `notes` gains `over-cap`. With the current append-only API, the already-written row cannot be amended. The card does not specify whether detection moves into `SessionRunner`, whether the runner receives the cap, or whether a second record is written.

This must be resolved in the card design; otherwise the acceptance criterion is structurally impossible without an unplanned API change.

### F3 — T42’s exception does not carry all required handoff data

**Severity: high**

The proposed `OverContextBudget(peak_tokens, limit, stage, task_id)` lacks:

- slice id;
- iteration;
- `out_file`;
- enough structured context to create the required handoff.

Yet the required park reason and handoff include stage, slice, iteration, last output path, and checkpoints. `_run` receives these values, but the card does not require them on the exception or define another structured transfer. Extending `park()` based only on a reason string would require brittle parsing and still cannot reliably obtain `out_file`.

### F4 — T22’s workdir flow is circular

**Severity: high**

Current order:

1. `Pipeline.process()` calls `lifecycle.intake(task)`.
2. `intake()` creates `original.md`.
3. `process()` calls `resolve_workdir(task_dir)`, which reads `original.md`.

T22 asks `intake()` to record “the resolved workdir it was given” and asks `pipeline.process()` to pass it. But the current resolver needs the task directory and persisted `original.md`, which do not exist until intake completes.

The card must define a concrete order, such as resolving directly from `task.body` before intake or intaking first and immediately persisting the resolved value. As written, a card agent must invent behavior.

### F5 — T13/T14 classify inaccessible claims as work

**Severity: high operational concern**

The cycle decision says `claims > 0 -> WORK`. T14 then spawns `run-task-loop --continue`. T12’s automatic stale reclaim is off by default, and `run-task-loop` cannot process files that remain in `claimed/`. Therefore:

- pending = 0;
- in-flight = 0;
- claimed > 0;
- action = WORK forever;
- spawned command performs no work forever.

The plan acknowledges this as a D4 caveat and relies on T15 backoff. That makes the loop quieter, not correct. Calling claims “work” is false unless the selected command can consume or requeue them. If this blocked state is intentionally preserved until human review, it should be represented as an explicit blocked/operator-required state rather than `WORK`.

### F6 — T15’s count tuple is not a reliable progress signal

**Severity: medium**

`progressed = after != before` compares only `(pending, in_flight, claims)` counts. Real progress can leave counts unchanged:

- one active task completes while another becomes active;
- one pending task is consumed while autonomous generation adds one;
- a claim is replaced by another claim;
- task identity changes without count changes.

This can produce false “no progress” backoff. It is not destructive, but the reasoning that count equality means no work occurred is incorrect.

### F7 — T32 requires logging from a config object with no logger

**Severity: medium**

T32 requires `model_context()` to log a warning for an unknown model. `Config` currently has no log dependency and is a plain dataclass. The card neither adds a logger nor specifies where the warning should be emitted. A fresh agent must either introduce hidden output (`print`), add an unplanned dependency, or ignore the requirement.

### F8 — T04 assumes pre-merge cleanliness is sufficient to delete all new untracked paths

**Severity: medium/high**

The proposed cleanup removes every `??` path after a failed squash on the reasoning that a clean pre-merge tree proves all such paths came from the merge. That is only true without concurrent writers and without files generated between the cleanliness check and cleanup. The harness runs external model/code processes in the same worktree and has no repository lock.

Deletion should be based on a pre-merge snapshot/delta, not merely the assertion that the tree was clean at one earlier instant. Directory, symlink, and nested untracked-path handling also needs explicit semantics.

### F9 — T27’s branch cleanup ordering is underspecified

**Severity: medium**

The card says branch deletion happens only after `complete()` and cannot fail the task, but offers two materially different placements: inside `complete()` or via a helper called after `complete()`. `complete()` currently has no workdir/trunk inputs, and after it moves the task, active-path state lookup changes. This should be decided in the card rather than delegated to the implementer, especially because the crash windows differ.

## 3. Verification and execution concerns

### C1 — Multiple verify blocks are not runnable as written

**Severity: high process concern**

Examples:

- T21/T23/T25/T42 instantiate `Config` with signatures that do not match the current dataclass and say “adapt” inline.
- T42 contains literal `...` placeholders and explicitly requires adaptation.
- T22’s `load_state` probe conditionally avoids making the call and can pass without testing loading behavior.
- Several cards rely on source substring ordering rather than executing behavior.

The plan says verify blocks are executable contracts for fresh card agents. Blocks requiring design-time adaptation are not executable contracts and create inconsistent implementation/testing across sessions.

### C2 — Source-text assertions can pass while behavior is wrong

**Severity: medium**

Examples include checking for symbol names, substrings, or absence of literals in T14, T15, T22, T26, T29, T30, T31, and T42. These are useful supplemental checks but often stand in for behavior. In particular:

- T14 does not execute one decision-to-spawn mapping;
- T15 does not exercise before/after progress tracking;
- T26’s shown parser assertion can avoid invoking the parser depending on implementation shape;
- T42 checks source ordering and then leaves runtime scaffolding to adaptation.

The permanent tests partly compensate, but not for every behavior.

### C3 — T38 does not test supervisor wiring despite being cited as its proof

**Severity: high**

T16 says the supervisor contract is “proven by card T38’s cycle test.” T38 explicitly must not import or execute `supervisor.py`; it tests only the pure decision module and possibly source AST. It therefore cannot prove that the supervisor actually spawns `run-task-loop --continue`, passes the right counts, applies backoff correctly, or handles return codes.

This is a documentation/reasoning mismatch: the test proves decision helpers, not integration wiring.

### C4 — T34’s parser expectation for unknown vocabulary is ambiguous

**Severity: medium**

T19’s regex deliberately accepts any letters/underscore and returns the raw lowercased token. T20 maps out-of-vocabulary values to `Verdict.UNKNOWN`. T34 says `VERDICT: kick_out -> unknown at the parse layer if kick_out is outside the vocabulary — assert whatever the code does`.

The parse layer is lexical, not vocabulary-aware, under T19. “Assert whatever the code does” weakens the contract and risks pinning accidental behavior. The card should clearly separate lexical extraction (`kick_out`) from semantic mapping (`UNKNOWN`).

### C5 — Test ownership is fragmented and delayed

**Severity: medium**

Most high-risk fixes are verified initially by ad hoc snippets, with permanent regression tests deferred to wave 9. If a later card changes the same module before wave 9, the only protection is the global pre-existing suite. Pulling individual test cards forward is permitted, but the dependency/index still presents tests as a late wave.

For destructive git and subprocess changes, permanent tests should land with or immediately after their subject cards, as T23/T27 already correctly require for otherwise-unowned behavior.

### C6 — Gate mutates production-adjacent state

**Severity: medium**

The global Gate runs `python3 harness.py status`. After T07, every status invocation appends to the real `work/logs/harness.log`; composition also creates queue/log directories and initializes the stats file if absent. This means the universal gate is no longer read-only and every card run changes external operational state.

The plan records a narrow exception, but repeated gate execution can rotate logs and complicate evidence. This is especially concerning for a hardening gate intended to be deterministic.

## 4. Additional concerns about card boundaries and dependencies

- **T20/T29 type transition needs one explicit API contract.** `SessionResult.verdict` is currently annotated `str`; T20 appears to return `Verdict`, while stats require strings. T29 tells the implementer to inspect and choose. The type should be decided in T20 so T29 is truly mechanical.
- **T21’s missing-file requirement conflicts with `load_state()` behavior.** `load_state()` documents that missing files raise `FileNotFoundError`; T21 says it is “already tolerant” and asks terminal moves to construct minimal state. The card needs an explicit catch path rather than implying current tolerance covers missing files.
- **T25’s orphan-claim definition is weak.** A claimed file normally has no matching pending file by design, so “slug matches no pending/active task” may classify every legitimate queued claim as anomalous. The report should distinguish expected claimed-only state from genuinely inconsistent duplication/ownership evidence.
- **T02/T07 rotation caps are approximate in characters, not encoded bytes.** `len(line)` does not equal bytes written for non-ASCII text and appears not to include timestamp/newline overhead. This is acceptable for a soft cap but should not be described as an exact byte bound.
- **T40’s `requires-python = ">=3.14"` records the current interpreter rather than the minimum language requirement.** That may unnecessarily prevent use on compatible earlier Python versions. If 3.14 is truly an operational requirement, the basis should be stated; “measured 3.14.7” alone does not establish a minimum.

## 5. Positive observations

- The plan correctly prioritizes rollback safety, observability, claim recovery, subprocess deadlocks, and state truthfulness before style cleanup.
- T03–T06 show strong attention to destructive git failure modes and explicitly reject unsafe `reset --hard` behavior.
- T17–T20 correctly separate transport/process failure, stderr, lexical verdict parsing, and semantic verdict mapping.
- T23/T24 correctly fail closed around queue-local repositories and unknown verification gates.
- T26/T27 address the two major checkpoint replay windows and require dedicated tests where wave-9 ownership would otherwise be absent.
- The plan repeatedly protects the real queue/stats/logs and uses temp repositories/fake executables for dangerous boundaries.
- Open human decisions are recorded rather than silently converted into implementation assumptions.

## Recommended disposition before execution

Resolve the critical/high items in the card text before running the sequence, especially:

1. assign and test fail-closed specification-assessor routing;
2. redesign T42 so enforcement occurs while stream events are being consumed and define how stats/handoff metadata flows;
3. make T22’s intake/resolution order concrete;
4. represent the D4 claimed-only condition honestly rather than routing it to an inoperative `WORK` command;
5. replace placeholder/adaptive verify blocks with exact runnable tests against the real constructors and APIs.
