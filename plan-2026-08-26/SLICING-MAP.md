# Recursive Slicing Map

Parent files remain as requirement archives and must not be enqueued when marked **DO NOT EXECUTE**.

| Parent | Executable leaves | Order |
|---|---|---|
| T04 squash failure cleanup | T72, T73 | T72 → T73 |
| T27 merge checkpoint/branch lifecycle | T70, T71 | T70 → T71 |
| T42 over-cap enforcement | T48, T49, T74, T75 | T48 → T49 → T74 → T75 |
| T46 claim ownership | T51, T52, T53 | T51 → T52; T51 → T53 |
| T41 small items | T54, T55, T56, T57, T58 | independent after each leaf's stated dependencies |
| T33 units/config/docs | T33, T59 | T33 code/config first; T59 only after all documented behavior lands |
| T25 queue audit | T76, T77, T61 | T76 → T77 → T61 |
| T36 git tests | T62, T63, T64, T65 | independent after subject implementation dependencies |
| T37 handler tests | T58, T66, T67, T68 | independent after subject implementation dependencies |
| T40 project/gate/docs | T40, T69, T59 | T40 → T69; T59 last |

## Second-generation parents

A leaf that was itself re-sliced becomes an archive on the same terms as a first-generation parent:
its card carries the line-3 **DO NOT EXECUTE** directive naming its children, and the enqueue guard
refuses it.

| Parent (was a leaf) | Executable leaves | Order |
|---|---|---|
| T50 over-cap park + handoff | T74, T75 | T74 → T75 |
| T60 read-only queue-audit core | T76, T77 | T76 → T77 |

## Re-slice audit (2026-08-26)

Every open leaf in the tables above was measured against the hard limits in
`RECURSIVE-SLICING-ALGORITHM.md`. Read sets are the card's own `Read first` files, counted in lines as
they exist in the tree (`config.json` counted at `HEAD` — it is currently deleted in the working tree
by an unrelated in-flight edit, which is why the local Gate is red). "Modules" counts production
modules the `Do` edits, plus the one test module it creates.

| Leaf | Read set | Modules | Criteria | Verdict |
|---|---|---|---|---|
| T33 | 3 files / 293 | `session.py`, `config.json` | 4 | normalized — step 4 read "if no existing test executes the line"; no test does, so it now names `tests/test_log_units.py` unconditionally |
| T40 | 1 file / 95 | `pyproject.toml` | 4 | normalized — `Read first` was "Python imports under `harness/`, `external/`, …", an unbounded read set; replaced by a `grep` discovery step |
| T48 | 1 / 224 | `pi_cli.py` + test | 3 | normalized — renamed its suite; `tests/test_pi_subprocess.py` belongs to T35, so the two leaves could not revert apart |
| T49 | 1 / 123 | `session.py` + test | 3 | fits |
| T51 | 1 / 236 | `providers.py` + test | 5 | fits |
| T52 | 1 / 332 | `handlers.py` + test | 2 | normalized — T52 and T61 both created `tests/test_handlers.py`; now `tests/test_run_owner_id.py` |
| T53 | 2 / 568 | `providers.py`, `handlers.py` + test | 4 | fits (2 production modules is the ceiling, not over it) |
| T54–T58 | 1–3 / ≤442 | ≤2 + test | 2–3 | fit |
| T59 | 3 / 272 | documentation only | 2 | fits — the map keeps all documentation in one leaf |
| T62, T63, T65 | 2 / 548 | test only | 2 | fit |
| T64 | 2 / 548 | test only | 2 | normalized — its target module already exists (T72 landed it); "create" replaced by "extend" |
| T66, T67 | 2 / 568 | test only | 2 | fit |
| T68 | 3 / 505 | test only | 2 | fit |
| T69 | 2 / 469 | `scripts/gate.sh` + test | 3 | normalized — `Read first` named a file that no longer exists at that path, and "test … where practical" was an unresolved design choice; the fixture is now decided (temp stub directory, always) |
| T70, T71 | 2 / ≤724 | ≤2 + test | 2–3 | fit |

Splits forced by the limits:

- **T50 → T74 → T75.** It crossed two partitions (`fits()` Q1: workflow routing *and*
  persistence/rendering), touched two production modules plus a test module, and its `Do` carried five
  separate behaviors. Routing lands first, per the algorithm's partition order.
- **T60 → T76 → T77.** Its `Do` listed seven unrelated anomaly classes — over the five-criterion
  ceiling — and `fits()` Q8 applies: an agent could land the queue-git check and silently regress the
  status check. Split at the walk boundary: T76 reports what the task-dir walk already sees (counts,
  rows, `.git`, `task.json`, status/location), T77 adds the checks that need something else and the
  operator footer.

Dependents were re-pointed at the leaf that actually owns the API they consume: T57 → T74, T59 → T75,
T61 → T77. `T42`'s and `T25`'s directives now name the new sequences, and the directives name only
executable ids — naming a superseded leaf there would tell an operator to enqueue a refused file.

Out of scope for this audit: the single-origin tickets that are not leaves of a marked parent
(T17–T31, T34, T35, T38, T39, T43–T47). Four of them measure over the limits (T20 and T43 at 8 criteria,
T38 at 6, T31 at 7 read files / 1261 lines) and several name plan files that moved to
`plan-2026-08-26-done/`; they need their own slicing pass and are not part of this map's leaf sets.

## Coverage check

- T04 separates conflict cleanup from commit-command failure cleanup.
- T27 separates checkpoint routing from post-completion branch deletion.
- T42 mechanism → propagation → policy/persistence is covered exactly once.
- T46 schema → run ownership → stale/operator policy is covered exactly once.
- Each T41 behavior has one leaf.
- T33 and T40 retain code/config ownership; all documentation is consolidated in T59.
- T25 separates pure read-only analysis from the only report write.
- T36 uses one fixture class per leaf.
- T37 separates claim handlers, run cleanup, parser reachability, and autonomous counting.
- T50's park routing (T74) and its handoff rendering (T75) are covered exactly once, and the routing
  leaf lands before the rendering leaf.
- T60's inventory/state anomalies (T76) and its artifact/duplicate-slug/claim/body anomalies plus the
  operator footer (T77) are covered exactly once, and every anomaly class from T25 survives in exactly
  one of them.
- No two executable leaves create the same file; each creates exactly one test module named for its
  behavior, so any leaf reverts without reverting a sibling.
- Every `Read first` path in a governed leaf exists in the tree and totals at most 4 files / 1200 lines.

## Enqueue rule

A card executor must reject any parent marked **DO NOT EXECUTE** with `VERDICT: kickout` and name the
leaf sequence from this map. A slicing agent must apply `/home/donald/work/harness/RECURSIVE-SLICING-ALGORITHM.md`
and recursively split a leaf again if its actual code has grown beyond the hard limits.
