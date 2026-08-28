"""The spec-assessor acceptance protocol (T43): what it takes to approve a spec.

`Pipeline.stage_spec` used to single out `KICKBACK` and let everything else
fall through to `spec approved`, so an assessor session that errored, said
nothing decidable, or emitted a verdict the assessor prompt does not even
define still waved the specification through. The rule is stated here once so
both assessors — Ornith and the technical-writer requirement check — are held
to it identically:

- `Verdict.PASS` from a session that finished cleanly is the only approval;
- `Verdict.KICKBACK` is the revision-loop signal (the pipeline owns the loop,
  its counter and its maximum);
- every other verdict parks the task, fail closed.

A process failure is never reinterpreted as a content verdict: `ok` is checked
before the verdict, so a session that died or exited non-zero cannot approve a
specification even when its partial output carries a `VERDICT: pass` line.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.enums import Verdict
from ..core.session import SessionResult


class SpecAssessment(Enum):
    """How one assessor session routes the spec stage.

    Never on the wire — it is a routing outcome inside the pipeline, not a
    verdict a model emits, so it lives beside its only consumer rather than in
    `core.enums` (whose values are load-bearing history).
    """
    APPROVED = "approved"
    KICKBACK = "kickback"
    PARKED = "parked"


@dataclass(frozen=True)
class SpecAssessmentDecision:
    """The routing outcome for one assessor session.

    `reason` is filled only for `PARKED`: it names the assessor and the verdict
    so the parked task says out loud who refused and why.
    """
    outcome: SpecAssessment
    assessor: str
    verdict: Verdict
    reason: str = ""


def assess_spec(assessor: str, result: SessionResult) -> SpecAssessmentDecision:
    """Classify one spec-assessor session against the acceptance protocol."""
    if not result.ok:
        return SpecAssessmentDecision(
            SpecAssessment.PARKED, assessor, result.verdict,
            f"spec assessor {assessor} did not complete cleanly "
            f"(process failure, verdict={_verdict_text(result.verdict)}); "
            f"a failed session is not a content verdict and cannot approve "
            f"a specification")
    if result.verdict is Verdict.PASS:
        return SpecAssessmentDecision(SpecAssessment.APPROVED, assessor,
                                      result.verdict)
    if result.verdict is Verdict.KICKBACK:
        return SpecAssessmentDecision(SpecAssessment.KICKBACK, assessor,
                                      result.verdict)
    return SpecAssessmentDecision(
        SpecAssessment.PARKED, assessor, result.verdict,
        f"spec assessor {assessor} returned "
        f"{_verdict_text(result.verdict)}, which does not approve a "
        f"specification (only PASS approves, KICKBACK revises)")


def _verdict_text(verdict: Verdict) -> str:
    """The verdict's wire value. A stray non-member is rendered verbatim: a
    park reason must never be the thing that raises."""
    return verdict.value if isinstance(verdict, Verdict) else str(verdict)
