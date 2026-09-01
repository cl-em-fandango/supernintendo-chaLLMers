"""Slice-7 tests: the operator-facing `--help` contract for interrupts.

The interrupt mechanism fails safe by design, so the recovery paths must be
readable *before* the operator is stuck. These tests pin the help text that
the spec makes binding:

  * FR-2.5/C4 — quick mode documents that a requester killed before cleanup
    leaves the file in place and that `harness.py resume` is the recovery,
    and that the borrowed session runs in the harness' container context via
    `scripts/harness-run` (TTY, model endpoints);
  * FR-1.1 — `--stand-down` documents the same killed-requester recovery;
  * FR-6.5 — neither `--requeue-stale` nor `resume` may read as if a
    stand-down reclaims claims: the stand-down duration never makes the
    operator's own claims reclaimable by itself, `resume` never reclaims
    implicitly, and age-based reclaim stays an explicit operator action
    (`--requeue-stale`, `requeue-claims`);
  * FR-2.3 — `--model` help states the concrete-model rule (pool names
    rejected) and the `models.technicalWriter` default.

FR-1.5 (the container gate) is covered by `tests/test_environment_gate.py`,
which pins `assert_containerized()` as the first call in `harness.py main()`
for every subcommand, `interrupt` included. No temp dirs needed: this file
only reads the parser.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli.parser import build_parser  # noqa: E402


def _subparser(name: str):
    parser = build_parser()
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        return action.choices[name]
    raise AssertionError("parser has no subcommands")


def _help_text(name: str) -> str:
    # Whitespace is collapsed: argparse wraps help at the terminal width.
    return " ".join(_subparser(name).format_help().lower().split())


def _flag_help(name: str, dest: str) -> str:
    for action in _subparser(name)._actions:  # noqa: SLF001
        if getattr(action, "dest", None) == dest:
            return action.help or ""
    raise AssertionError(f"{name} has no --{dest}")


class TestInterruptHelp(unittest.TestCase):
    """`interrupt --help` carries the recovery and container/TTY notes."""

    def setUp(self) -> None:
        self.help = _help_text("interrupt")  # already lower-collapsed

    def test_killed_requester_recovery_is_documented(self):
        # FR-2.5/E3: killed before cleanup -> file remains -> resume.
        lowered = self.help
        self.assertIn("killed", lowered)
        self.assertIn("harness.py resume", lowered)
        self.assertIn("remains", lowered)

    def test_container_and_tty_context_is_documented(self):
        # C4: the quick session needs the harness' container context and
        # the operator's TTY, reached via scripts/harness-run.
        lowered = self.help
        self.assertIn("scripts/harness-run", lowered)
        self.assertIn("container", lowered)
        self.assertIn("tty", lowered)

    def test_stand_down_flag_documents_recovery(self):
        # FR-1.1: the stand-down flag's own help repeats the recovery line.
        flag = _flag_help("interrupt", "stand_down").lower()
        self.assertIn("killed", flag)
        self.assertIn("harness.py resume", flag)
        self.assertIn("checkpoints", flag)

    def test_model_flag_documents_concrete_model_rule(self):
        # FR-2.3/E7: pools rejected, default models.technicalWriter.
        flag = _flag_help("interrupt", "model").lower()
        self.assertIn("pool", flag)
        self.assertIn("rejected", flag)
        self.assertIn("models.technicalwriter", flag)


class TestRequeueStaleStaysOptIn(unittest.TestCase):
    """FR-6.5: a stand-down never reclaims claims by itself."""

    def test_requeue_stale_help_excludes_stand_down_duration(self):
        for command in ("run", "run-task-loop"):
            with self.subTest(command=command):
                flag = _flag_help(command, "requeue_stale").lower()
                self.assertIn("stand-down", flag)
                self.assertIn("opt-in", flag)
                self.assertIn("claim", flag)

    def test_resume_help_denies_implicit_reclaim(self):
        # FR-6.5: `resume` must not read as a stale-claim reclaim.
        # Whitespace is collapsed: argparse wraps help at its terminal width.
        resume = _help_text("resume")
        self.assertIn("ever reclaims stale claims implicitly", resume)
        self.assertIn("requeue-claims", resume)


if __name__ == "__main__":
    unittest.main()
