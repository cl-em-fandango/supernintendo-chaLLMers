# Recursive Ticket Slicing Algorithm

## Objective
Turn one implementation ticket into an ordered set of tickets that each fit one fresh local-model context while preserving all original acceptance criteria.

## Hard limits per leaf ticket
A leaf is acceptable only when all are true:

- touches at most **2 production modules** or **1 production module plus 1 test module**;
- changes one behavior or one boundary contract;
- has at most **5 acceptance criteria**;
- has one executable verify block with no placeholders or “adapt as needed” text;
- has no unresolved design choice (`pick one`, `if useful`, `whatever the code does`);
- has no dependency on an unlanded symbol except a named predecessor ticket;
- can be reverted without reverting a sibling leaf;
- estimated read set is at most **4 files / 1,200 lines**;
- estimated implementation is at most **250 changed lines**, including tests;
- contains no real queue, stats, log, model, or supervisor mutation.

The numeric limits are conservative heuristics, not goals. A destructive subprocess boundary should be split even below the limits when it combines detection, policy, and recovery.

## Algorithm

```text
slice(ticket, inherited_contract):
    normalize(ticket)
    reject unresolved decisions and non-runnable verification
    contract = inherited_contract ∪ ticket.acceptance_criteria

    graph = build_change_graph(ticket)
      nodes = observable behaviors, schemas, process boundaries, persistence writes,
              policy decisions, CLI wiring, and tests
      edges = “must land before” dependencies

    components = partition graph in this order:
      1. boundary mechanism (subprocess/filesystem/git)
      2. typed data propagation
      3. workflow policy/routing
      4. persistence/rendering
      5. CLI/composition wiring
      6. permanent regression tests

    merge adjacent components only when separating them would require a temporary
    invalid API or an untestable intermediate commit

    for each component in topological order:
        leaf = make_ticket(component)
        leaf.acceptance = subset of contract owned by component
        leaf.verify = executable proof of only that subset
        leaf.out_of_scope = contract owned by siblings + unrelated behavior
        leaf.dependencies = immediate predecessor APIs only

        if not fits(leaf):
            slice(leaf, leaf.acceptance)
        else:
            emit leaf

    validate_partition(original, leaves)
```

## `fits()` decision procedure
Answer in order; any “yes” forces another split.

1. Does it cross more than one of: boundary mechanism, policy, persistence, CLI, documentation?
2. Does it introduce a data structure and all of its consumers in the same ticket?
3. Does it both fix behavior and add a broad regression suite?
4. Does it contain two independently revertible behaviors?
5. Does its verify block need more than one fixture type (for example fake process and temp git repo)?
6. Does it require reading files not listed in `Read first`?
7. Does it use source-text assertions as the sole proof of runtime behavior?
8. Could an agent satisfy one acceptance criterion while silently breaking another unrelated one?
9. Is any step conditional on repository state rather than an explicit dependency?
10. Would failure require a handoff that says “part A works, part B remains”?

## Partition validation
Before accepting the slice set:

- every original acceptance criterion has exactly one owning leaf;
- every original out-of-scope item remains out of scope;
- no leaf relies on a later leaf;
- intermediate commits import and pass the global gate;
- API-producing leaves precede API-consuming leaves;
- every destructive behavior has a permanent temp-fixture regression test no later than the immediately following leaf;
- documentation lands only after the behavior it documents;
- the final leaf runs the original ticket’s end-to-end acceptance test, if one exists.

## Prompt for the local slicer

```text
Read the ticket and only its Read-first files. Do not implement it.
Build a change graph with nodes for boundary mechanism, typed result propagation,
workflow policy, persistence, CLI wiring, documentation, and tests. Partition it
into topologically ordered leaf tickets. Recursively split every leaf that violates
any hard limit in RECURSIVE-SLICING-ALGORITHM.md. Preserve every original acceptance
criterion exactly once. Resolve no product decisions: report a blocking decision
instead. Write each leaf as an individual markdown file with Context, Read first,
Do, Verify, Out of scope, and Done when. Verify blocks must run as written and may
use only temp dirs/repos and fake executables. Output `VERDICT: done` only after the
partition-validation checklist passes.
```
