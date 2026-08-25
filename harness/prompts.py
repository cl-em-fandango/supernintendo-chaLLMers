"""Prompt builders. Each returns the full prompt for one pi session."""
from __future__ import annotations

from pathlib import Path

VERDICT_RULES = """
IMPORTANT OUTPUT PROTOCOL:
- Do your work using your tools as needed.
- At the very end of your final message, you MUST include a single line in exactly this format:
  VERDICT: <value>
- <value> must be one of the values listed in the VERDICT OPTIONS below.
- Before the VERDICT line, include a section headed "## Summary" with a concise summary of what you did or found. This is read by a human and by the next session.
"""

# Injected into every session that writes or modifies code, so all agents
# follow the same standards from the get-go. Kept short on purpose: the full
# document is CODING_STANDARDS.md in the repo root, which the session can read
# for detail.
CODING_STANDARDS_PREAMBLE = """
CODING STANDARDS (binding — read CODING_STANDARDS.md in the repo root for the
full version before writing code):
- One responsibility per file, named after that responsibility. No grab-bag modules.
- Split state from behavior: a @dataclass holds the shape of data; a separate
  module holds the functions that act on it. No tuples or bare dicts for
  meaningful state — every piece of state is a named class with typed fields.
- Enums instead of magic strings for discrete state (task status, verdicts).
  Strings only at the very edges (model output, git refs).
- Clear modular boundaries: all subprocess calls live in external/ behind small
  function signatures (external/pi_cli.py, external/git_cli.py). cli/ only
  parses and dispatches. workflow/ composes modules and takes an explicit
  parameters object. The top-level entry point is the single composition root
  with no business logic. Dependencies point one direction and never cycle.
- Explicit parameters objects, not long argument lists or module-level globals.
- Small, readable, boring functions. The next agent reads your code cold.
- snake_case functions/vars/modules, PascalCase classes, UPPER_SNAKE_CASE
  constants and enum members. Names say what a thing is, not its type.
"""


def spec_author(td: Path) -> str:
    return f"""You are a technical writer. A new task has been submitted.

Read the original task requirement at: {td}/original.md

Your job: analyze the requirement and flesh it out into a complete, unambiguous FUNCTIONAL SPECIFICATION.
- If the requirement is vague or one sentence, expand it: goals, scope, in-scope/out-of-scope, functional requirements, acceptance criteria, constraints, edge cases.
- If the requirement is already a proper spec, verify it is complete and fix gaps.
- Write the spec to: {td}/artifacts/spec.md
- The spec must be self-contained: an implementer should be able to build from it without seeing the original task.

VERDICT OPTIONS:
- done: spec is complete and written to spec.md
{VERDICT_RULES}"""


def spec_assess(td: Path, stage: str) -> str:
    if stage == "ornith":
        focus = ("Assess the functional spec for technical soundness, completeness, and quality. "
                 "Contribute concrete amendments directly into the spec (edit it in place) where you "
                 "see gaps, errors, or improvements.")
    else:
        focus = (f"Assess the functional spec AGAINST THE ORIGINAL REQUIREMENT (read {td}/original.md). "
                 "Verify the spec fully covers what was asked, nothing important was dropped or "
                 "distorted, and nothing was added that contradicts the requirement. Fix any "
                 "misalignments in the spec in place.")
    return f"""You are reviewing a functional specification.

Read the spec at: {td}/artifacts/spec.md
{focus}

If the spec is fundamentally broken (not salvageable by edits — wrong direction, incoherent, or missing the point entirely), kick it back to be rewritten.

VERDICT OPTIONS:
- pass: spec is good (you may have amended it in place)
- kickback: spec is fundamentally broken; explain exactly what is wrong in your Summary so the author can rewrite it
{VERDICT_RULES}"""


def feasibility(td: Path) -> str:
    return f"""You are an implementer assessing feasibility.

Read the approved functional spec at: {td}/artifacts/spec.md
Explore the codebase you are in to understand what exists.

Assess: can this spec be implemented in this codebase? Consider existing architecture, dependencies, and effort.

VERDICT OPTIONS:
- pass: feasible; note any implementation considerations in your Summary
- kickback: not feasible as specified; explain what must change in the spec (it will be sent back to the spec stage)
- kickout: this task should not be done at all (e.g. duplicates existing functionality, wrong for this codebase, or harmful); explain why
{VERDICT_RULES}"""


def slice(td: Path) -> str:
    return f"""You are an implementer planning delivery.

Read the approved functional spec at: {td}/artifacts/spec.md

Slice the spec into a list of ATOMIC, VERTICALLY-SLICED implementation chunks. Rules:
- Each slice must be a narrow, vertically-sliced piece of work (a thin end-to-end increment, not a horizontal layer).
- Each slice must be completable in ONE agent session with a 128k context window (hard ceiling; plan for ~100k).
- Slices must be ordered so each builds on the previous.
- If a slice cannot fit in one session, split it into nested sub-slices.

Write the slice plan to: {td}/artifacts/slices.md
Format: a numbered list. Each entry has:
  ### Slice N: <title>
  - Goal: <one line>
  - Scope: <what files/areas it touches>
  - Done when: <concrete, verifiable completion criteria>
  - (if nested) Sub-slices N.1, N.2, ...

VERDICT OPTIONS:
- done: slice plan written
{VERDICT_RULES}"""


def slice_check(td: Path) -> str:
    return f"""You are checking a slice plan for session fit.

Read the slice plan at: {td}/artifacts/slices.md
Read the spec at: {td}/artifacts/spec.md for context.

For EACH slice (including sub-slices), judge: can it realistically be implemented in ONE fresh agent session with 128k context (plan for ~100k)? Consider code to read, code to write, and tests.

If any slice is too big, split it into nested sub-slices and rewrite slices.md with the corrected plan.

VERDICT OPTIONS:
- pass: every slice fits in one session
- resliced: you found slices that were too big and rewrote slices.md with sub-slices
{VERDICT_RULES}"""


def implement_slice(td: Path, sid: str, iteration: int, max_iter: int) -> str:
    progress = ""
    if iteration > 1:
        progress = f"""
This is iteration {iteration} of up to {max_iter}. A previous session did not finish this slice.
Read the progress note at: {td}/artifacts/progress/slice-{sid}.md
Continue from where it left off. Do NOT redo completed work.
"""
    return f"""You are implementing one slice of a feature.

Read the spec: {td}/artifacts/spec.md
Read the slice plan: {td}/artifacts/slices.md
Your slice: {sid}
{progress}
{CODING_STANDARDS_PREAMBLE}
Implement slice {sid} completely, including its tests, per its "Done when" criteria.
Work in the current repository on the current branch. Run the tests. Commit your work with a clear message when the slice is complete.

If you are running low on context or cannot finish in this session, STOP cleanly and write a progress note to: {td}/artifacts/progress/slice-{sid}.md
The progress note must contain: what is done (files changed, decisions made), what remains, and exactly what the next session should do first.

VERDICT OPTIONS:
- done: slice fully implemented, tested, and committed
- progress: not finished; progress note written for the next session
{VERDICT_RULES}"""


def fix_slice(td: Path, sid: str, feedback_file: Path, kind: str) -> str:
    extra = " so the slice delivers what the spec promises" if kind == "func" else ""
    return f"""You are fixing {kind} issues in slice {sid} found by {kind} review.

Read the review feedback at: {feedback_file}
Read the spec: {td}/artifacts/spec.md
{CODING_STANDARDS_PREAMBLE}
Fix the listed issues{extra}, run tests, and commit.

VERDICT OPTIONS:
- done: all review issues fixed and committed
{VERDICT_RULES}"""


def tech_review(td: Path, sid: str) -> str:
    return f"""You are a technical reviewer.

Read the spec: {td}/artifacts/spec.md
Read the slice plan: {td}/artifacts/slices.md
Slice under review: {sid}

Review the committed implementation of this slice (use git log/diff to see what was done).
Check: does it match the spec and the slice's "Done when" criteria? Is the code sound? Is test coverage appropriate?

VERDICT OPTIONS:
- pass: implementation is correct and adequately tested
- fail: problems found; list them precisely in your Summary (the implementer session will receive them)
{VERDICT_RULES}"""


def func_review(td: Path, sid: str) -> str:
    return f"""You are a functional reviewer (technical writer).

Read the spec: {td}/artifacts/spec.md
Read the slice plan: {td}/artifacts/slices.md
Slice under review: {sid}

Review the committed implementation of this slice FUNCTIONALLY: does it deliver what the spec promises for this slice? Would a user of this feature get the behavior the spec describes? Ignore code style — focus on functional correctness vs. the spec.

VERDICT OPTIONS:
- pass: functionally correct per spec
- fail: functional gaps found; describe them precisely in your Summary (the implementer session will receive them)
{VERDICT_RULES}"""


def holistic_review(td: Path) -> str:
    return f"""You are a functional reviewer (technical writer) doing a final holistic review.

Read the spec: {td}/artifacts/spec.md
Read the slice plan: {td}/artifacts/slices.md

All slices are implemented and committed on this branch. Review the feature as a WHOLE:
- Does the combined implementation deliver the full spec?
- Do the slices fit together coherently (no gaps between them)?
- Are the acceptance criteria in the spec met?

Use git log/diff to review the full branch.

VERDICT OPTIONS:
- pass: the feature as a whole meets the spec
- fail: gaps or incoherence found; describe them precisely in your Summary
{VERDICT_RULES}"""


def autonomous_suggest() -> str:
    return """You are analyzing this codebase to propose a new feature.

Explore the repository (read the code, README, tests, git history). Read CODING_STANDARDS.md in the repo root — proposals must be work that fits those standards. Identify a genuinely useful feature or improvement that is missing. It should be:
- Meaningful (not a trivial tweak)
- Appropriate to this codebase's purpose
- Implementable in a series of small sessions

Write your proposal to stdout as a task description. Include: a title, what the feature is, why it is useful, and rough scope. This text becomes a task in a work queue.

VERDICT OPTIONS:
- done: proposal written
""" + VERDICT_RULES


def autonomous_review(suggestion_file: Path) -> str:
    return f"""You are reviewing a proposed feature for this codebase.

Read the proposal at: {suggestion_file}
Explore the repository to judge it.

Reject the proposal if it is: dumb, trivial, already implemented, off-purpose for this codebase, or would cause harm. Approve only if it is a genuinely useful, non-duplicate feature.

VERDICT OPTIONS:
- pass: good proposal, add it to the queue
- reject: not worth doing; explain why in your Summary
{VERDICT_RULES}"""
