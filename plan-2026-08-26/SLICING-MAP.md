# Recursive Slicing Map

Parent files remain as requirement archives and must not be enqueued when marked **DO NOT EXECUTE**.

| Parent | Executable leaves | Order |
|---|---|---|
| T04 squash failure cleanup | T72, T73 | T72 → T73 |
| T27 merge checkpoint/branch lifecycle | T70, T71 | T70 → T71 |
| T42 over-cap enforcement | T48, T49, T50 | T48 → T49 → T50 |
| T46 claim ownership | T51, T52, T53 | T51 → T52; T51 → T53 |
| T41 small items | T54, T55, T56, T57, T58 | independent after each leaf's stated dependencies |
| T33 units/config/docs | T33, T59 | T33 code/config first; T59 only after all documented behavior lands |
| T25 queue audit | T60, T61 | T60 → T61 |
| T36 git tests | T62, T63, T64, T65 | independent after subject implementation dependencies |
| T37 handler tests | T58, T66, T67, T68 | independent after subject implementation dependencies |
| T40 project/gate/docs | T40, T69, T59 | T40 → T69; T59 last |

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

## Enqueue rule

A card executor must reject any parent marked **DO NOT EXECUTE** with `VERDICT: kickout` and name the
leaf sequence from this map. A slicing agent must apply `/home/donald/work/harness/RECURSIVE-SLICING-ALGORITHM.md`
and recursively split a leaf again if its actual code has grown beyond the hard limits.
