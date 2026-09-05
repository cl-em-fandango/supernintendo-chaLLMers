"""T49: the over-cap trip reaching one stats row (`harness/core/session.py`).

T48 taught `run_pi_session` to stop a session on the first streamed usage value
over the ceiling, but it kept that decision to itself: `SessionRunner.run` never
handed a ceiling down, so in a real session the check never ran at all, and the
trip never reached `SessionResult` or `sessions.jsonl`. An over-cap stop was
therefore invisible to stats — readable only in the raw session log — and T74's
park routing had nothing to route on.

This module owns the propagation, in three parts:

- the configured cap (`Config.max_prompt_tokens`, the one threshold decision D2
  names) is passed to `run_pi_session` as `max_context_tokens`. It is the *cap*,
  not the per-model `model_budget()`: the budget is the number the prompt tells
  the model to aim under and is smaller for a 32k/64k model, while the cap is
  the hard stop for a session that ignores it;
- `over_context_budget` and `context_limit` are copied onto `SessionResult`, and
  the trip stays distinct from `crashed` — a session we stopped on purpose is
  not a child that died, and the child's own return code is preserved;
- the *same* `SessionRecord` gains `over-cap peak=<n> limit=<n>` in its `notes`
  before the single append. One invocation, one row: no duplicate row and no
  rewrite of the JSONL.

Only the propagation is under test. The stream trip itself is
`tests/test_pi_over_cap_stream.py` (T48); the park routing is T74 and the handoff
markdown T75, so nothing here imports `harness/workflow/`.

`run_pi_session` is patched at `harness.core.session`, not at `external.pi_cli`:
`session.py` binds the name at import, so patching the defining module would
leave the runner calling the real subprocess. No subprocess, no model, no
network, no real `pi` binary.

Run from the repo root:  python3 -m unittest tests.test_over_cap_session
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage, Verdict
from harness.core.session import SessionResult, SessionRunner
from harness.core.stats import StatsStore
from external.pi_cli import PiSessionResult

# The configured ceiling (config.json `maxPromptTokens`) — the same literal
# boundary T48 asserts at the stream layer.
CAP = 60_000
OVER_CAP = 60_001

# A model whose window is smaller than the cap, used to prove *which* number is
# handed down: its `model_budget()` is 32768 - 8192 = 24576, so a runner that
# passed the per-model budget instead of the cap fails the assertion rather than
# passing by coincidence.
SMALL_WINDOW_MODEL = "SmallWindowModel"
SMALL_WINDOW_BUDGET = 24_576

# The annotation, exactly as it must read in the row: both numbers, so an
# operator can tell the measured peak from the ceiling that stopped the session.
ANNOTATION = f"over-cap peak={OVER_CAP} limit={CAP}"

# The return code of a child stopped by SIGTERM. Kept separate from `crashed`
# downstream: the trip is a budget decision, not a death.
SIGTERM_RC = 143


def _cfg(work_dir: Path) -> Config:
    """A hand-built config with the shipped 60,000-token cap."""
    return Config(
        harness_execution_and_queue_dir=work_dir,
        token_budget=CAP,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
        model_context_map={"m": 131_072, SMALL_WINDOW_MODEL: 32_768},
    )


class PiDouble:
    """A `run_pi_session` stand-in: records every call, returns a fixed result.

    The signature mirrors the real function, `max_context_tokens` included — a
    runner that stopped passing the cap would raise no TypeError here, so the cap
    is asserted explicitly instead (see `test_configured_cap_is_passed_to_pi_cli`).
    """

    def __init__(self, *, rc: int = 0, crashed: bool = False, err: str = "",
                 peak_tokens: int = 7, output: str = "## Summary\nworked\n\nVERDICT: done",
                 over_context_budget: bool = False,
                 context_limit: int | None = None) -> None:
        self.rc = rc
        self.crashed = crashed
        self.err = err
        self.peak_tokens = peak_tokens
        self.output = output
        self.over_context_budget = over_context_budget
        self.context_limit = context_limit
        self.calls: list[dict] = []

    def __call__(self, *, model: str, workdir: Path, prompt: str, out_file: Path,
                 log, max_context_tokens: int | None = None) -> PiSessionResult:
        self.calls.append({
            "model": model,
            "prompt": prompt,
            "out_file": Path(out_file),
            "max_context_tokens": max_context_tokens,
        })
        Path(out_file).write_text(self.output)
        return PiSessionResult(
            rc=self.rc,
            crashed=self.crashed,
            err=self.err,
            peak_tokens=self.peak_tokens,
            duration_s=0.1,
            output=self.output,
            out_file=Path(out_file),
            stderr="",
            over_context_budget=self.over_context_budget,
            context_limit=self.context_limit,
        )


class OverCapPropagationTest(unittest.TestCase):
    """One `SessionRunner.run` against one double: the cap down, the trip up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.cfg = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        # A list sink, so the runner's operator log never lands on the test
        # output; the lines themselves are T33's subject, not this leaf's.
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)

    def _run(self, pi: PiDouble, *, model: str = "m",
             notes: str = "") -> SessionResult:
        """`SessionRunner.run` with the module-level binding patched to `pi`."""
        with patch("harness.core.session.run_pi_session", pi):
            return self.runner.run(model, self.work_repo, "p", task_id="t1",
                                   stage=Stage.SLICE_IMPLEMENT, notes=notes)

    def _rows(self) -> list[dict]:
        return self.store.all()

    def _appends(self) -> list[str]:
        """The physical JSONL lines — the count a duplicate row would change."""
        return [ln for ln in self.cfg.stats_path.read_text().splitlines()
                if ln.strip()]

    # ------------------------------------------------------------------
    # a. the configured cap is handed to the stream layer
    # ------------------------------------------------------------------
    def test_configured_cap_is_passed_to_pi_cli(self):
        pi = PiDouble()

        self._run(pi, model=SMALL_WINDOW_MODEL)

        self.assertEqual(len(pi.calls), 1, "expected exactly one pi call")
        passed = pi.calls[0]["max_context_tokens"]
        self.assertEqual(passed, self.cfg.max_prompt_tokens)
        self.assertEqual(passed, CAP)
        # The cap, not the per-model budget: for this model the two differ, and
        # only the cap is the threshold decision D2 names.
        self.assertEqual(self.cfg.model_budget(SMALL_WINDOW_MODEL),
                         SMALL_WINDOW_BUDGET)
        self.assertNotEqual(passed, self.cfg.model_budget(SMALL_WINDOW_MODEL))

    # ------------------------------------------------------------------
    # b. the structured fields reach SessionResult, distinct from a crash
    # ------------------------------------------------------------------
    def test_over_cap_fields_reach_session_result(self):
        pi = PiDouble(
            rc=SIGTERM_RC,
            crashed=False,
            err=f"over context cap: peak={OVER_CAP} tokens limit={CAP} tokens",
            peak_tokens=OVER_CAP,
            output="partial work ",
            over_context_budget=True,
            context_limit=CAP,
        )

        result = self._run(pi)

        self.assertTrue(result.over_context_budget)
        self.assertEqual(result.context_limit, CAP)
        self.assertEqual(result.peak_tokens, OVER_CAP)
        # The trip is not a crash, and the child's own return code is preserved,
        # so T74 can route on the trip without losing the distinction.
        self.assertFalse(result.crashed)
        # A stopped session's partial text carries no verdict; the trip must not
        # invent one.
        self.assertEqual(result.verdict, Verdict.UNKNOWN)

    def test_within_cap_result_reports_the_limit_without_a_trip(self):
        """The ceiling in force is copied whether or not it was crossed."""
        pi = PiDouble(peak_tokens=CAP, context_limit=CAP)

        result = self._run(pi)

        self.assertFalse(result.over_context_budget)
        self.assertEqual(result.context_limit, CAP)

    # ------------------------------------------------------------------
    # c. one invocation, one annotated row
    # ------------------------------------------------------------------
    def test_one_over_cap_invocation_writes_exactly_one_annotated_row(self):
        pi = PiDouble(
            rc=SIGTERM_RC,
            err=f"over context cap: peak={OVER_CAP} tokens limit={CAP} tokens",
            peak_tokens=OVER_CAP,
            output="partial work ",
            over_context_budget=True,
            context_limit=CAP,
        )

        self._run(pi, notes="slice 1.1 attempt")

        rows = self._rows()
        self.assertEqual(len(rows), 1,
                         f"over-cap session wrote {len(rows)} rows, expected 1")
        self.assertEqual(len(self._appends()), 1,
                         "the annotation was written as a second append")
        row = rows[0]
        self.assertIn(ANNOTATION, row["notes"])
        # The caller's own notes survive: the annotation is added to the same
        # field, not written over it.
        self.assertTrue(row["notes"].startswith("slice 1.1 attempt"), row["notes"])
        # The row keeps the raw numbers too, so the annotation is a label on
        # data that is already there rather than a replacement for it.
        self.assertEqual(row["peak_tokens"], OVER_CAP)
        self.assertEqual(row["rc"], SIGTERM_RC)
        self.assertEqual(row["stage"], Stage.SLICE_IMPLEMENT.value)

    def test_session_within_the_cap_is_not_annotated(self):
        pi = PiDouble(peak_tokens=CAP)

        self._run(pi, notes="plain run")

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notes"], "plain run",
                         "a session that stayed inside the cap gained a note")

    def test_over_cap_and_crash_annotations_share_one_row(self):
        """Both anomalies land on the same record, in one append."""
        pi = PiDouble(
            rc=-15,
            crashed=True,
            err="pi did not exit after stdout closed",
            peak_tokens=OVER_CAP,
            output="partial work ",
            over_context_budget=True,
            context_limit=CAP,
        )

        self._run(pi)

        rows = self._rows()
        self.assertEqual(len(rows), 1,
                         f"two anomalies wrote {len(rows)} rows, expected 1")
        self.assertEqual(len(self._appends()), 1)
        self.assertIn(ANNOTATION, rows[0]["notes"])
        self.assertIn("[crashed: ", rows[0]["notes"])


if __name__ == "__main__":
    unittest.main()
