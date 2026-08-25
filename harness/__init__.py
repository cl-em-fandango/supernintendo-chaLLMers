"""Autonomous workflow harness for pi.

Pipeline per task:
  spec (author -> assess -> requirement-check, with kickbacks)
  -> feasibility (pass / kickback / kickout)
  -> slicing (+ recursive fit check)
  -> per-slice: implement (<=5) -> tech review (<=5) -> func review (<=5)
  -> holistic review -> squash-merge to trunk (or park)

All sessions are fresh, token-budgeted, and recorded in a unified stats store.
"""

__version__ = "0.1.0"
