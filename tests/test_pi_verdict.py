"""T34: table tests for verdict extraction (finding F11, F5).

`external/pi_cli.py` is the most crash-prone module in the repo and the verdict
parser at its end is the cheapest part of it to pin: `_extract_verdict` is pure
text in, string out, and it silently drove real retry/park loops — `unknown` is
21 of the 56 historical rows in `sessions.jsonl`. A wire the parser cannot read
is not a model failure, it is a harness failure, so the wire contract belongs in
a table.

Three layers are kept deliberately separate here, because conflating them is
what made `unknown` meaningless:
- `_extract_verdict` (lexical): returns *whatever token* the text carries, so an
  unsupported token comes back verbatim (`VERDICT: kick_out` -> `"kick_out"`);
- `Verdict.parse` (vocabulary): is that token one of the values we know;
- `_map_verdict` (semantics, T20): turn a parsed token plus the crashed flag
  into a `Verdict` member.

`_outcome` is pinned in the same file because it is pure and because the stats
report re-renders the historical rows: the outcome column must keep the exact
strings the 56 existing rows carry (`reject` rows already stored `unknown`, so
`reject` -> `unknown` is the compatible behaviour, not a bug).

No process is spawned and nothing is written to disk — everything that spawns a
process (stderr drain, watchdog, crash and rc handling) is T35.

Run from the repo root:  python3 -m unittest tests.test_pi_verdict
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.enums import Verdict
from harness.core.session import _outcome
from external.pi_cli import _extract_verdict

# 57 chars of neutral prose per line, 200 lines: a text big enough that a parser
# scanning only the head of the output would miss a verdict on the last line.
FILLER_LINE = "lorem ipsum dolor sit amet, consectetur adipiscing elit. "
LARGE_OUTPUT_WITH_VERDICT = "\n".join([FILLER_LINE] * 200) + "\nVERDICT: done"

# (text, expected) pairs. The label is the key and is also the test method name
# suffix (`test_<label>`), so `test_table_is_fully_covered` fails if a row is
# ever added to the table without a method of its own.
EXTRACT_CASES: dict[str, tuple[str, str]] = {
    # the wire format is `VERDICT: <token>`; the enum vocabulary is lowercase.
    "lowercase": ("VERDICT: done", "done"),
    # the bug T19 exists for: models emit the verdict uppercased constantly, and
    # a parser that only reads lowercase sent those runs to retry and park.
    "uppercase": ("VERDICT: DONE", "done"),
    "mixed_case": ("Verdict: Pass", "pass"),
    # trailing prose after the token is normal; the token ends at the space.
    "trailing_prose": ("VERDICT: pass — all good", "pass"),
    # JSON fallback: the quote after `verdict` blocks the plain `VERDICT:` line,
    # so the JSON pattern is what has to catch it.
    "json_fallback": ('{"verdict": "kickback"}', "kickback"),
    "json_in_prose": ('noise {"verdict": "KICKBACK"} more text', "kickback"),
    # a session that re-checks its work and changes its mind is normal.
    "last_line_wins": ("first VERDICT: fail\nthen a re-check\nVERDICT: pass",
                       "pass"),
    "no_verdict": ("I finished the work, committed it and moved the card.",
                   "unknown"),
    # the keyword alone carries no verdict: it must not become an empty token.
    "bare_marker": ("VERDICT: ", "unknown"),
    # lexical extraction only — vocabulary validation is `Verdict.parse`, and
    # the raw token has to survive so the stats notes can teach us it exists.
    "unsupported_token": ("VERDICT: kick_out", "kick_out"),
    "empty": ("", "unknown"),
    "large_output_last_line": (LARGE_OUTPUT_WITH_VERDICT, "done"),
}

# Verdict wire values present in the 56 historical rows of `sessions.jsonl`,
# mapped to the outcome they already carry there. The stats report re-renders
# those rows, so a changed string here is a silent history rewrite.
# (21 `unknown` + 2 `reject` verdicts == the 23 `unknown` outcomes on disk.)
OUTCOME_CASES: dict[str, str] = {
    "pass": "pass",
    "done": "done",
    "unknown": "unknown",
    "error": "error",
    "reject": "unknown",
    "kickback": "kickback",
}


class ExtractVerdictTest(unittest.TestCase):
    """`_extract_verdict` on its own: one string in, one string out."""

    def _check(self, label: str) -> None:
        text, expected = EXTRACT_CASES[label]
        self.assertEqual(_extract_verdict(text), expected, msg=label)

    def test_lowercase(self):
        self._check("lowercase")

    def test_uppercase(self):
        self._check("uppercase")

    def test_mixed_case(self):
        self._check("mixed_case")

    def test_trailing_prose(self):
        self._check("trailing_prose")

    def test_json_fallback(self):
        self._check("json_fallback")

    def test_json_in_prose(self):
        self._check("json_in_prose")

    def test_last_line_wins(self):
        self._check("last_line_wins")

    def test_no_verdict(self):
        self._check("no_verdict")

    def test_bare_marker(self):
        self._check("bare_marker")

    def test_unsupported_token(self):
        self._check("unsupported_token")

    def test_empty(self):
        self._check("empty")

    def test_large_output_last_line(self):
        self._check("large_output_last_line")
        # Guard the fixture itself, otherwise the case quietly shrinks to a
        # short text and stops testing what the card asked for.
        self.assertGreaterEqual(len(EXTRACT_CASES["large_output_last_line"][0]),
                                10_000)

    def test_table_is_fully_covered(self):
        for label in EXTRACT_CASES:
            with self.subTest(case=label):
                self.assertTrue(hasattr(self, f"test_{label}"),
                                f"table row {label!r} has no test method")


class OutcomeReportCompatibilityTest(unittest.TestCase):
    """`_outcome` keeps the historical outcome strings byte-identical."""

    def _check(self, verdict_value: str) -> None:
        self.assertEqual(_outcome(verdict_value),
                         OUTCOME_CASES[verdict_value], msg=verdict_value)

    def test_outcome_pass(self):
        self._check("pass")

    def test_outcome_done(self):
        self._check("done")

    def test_outcome_unknown(self):
        self._check("unknown")

    def test_outcome_error(self):
        self._check("error")

    def test_outcome_reject(self):
        # The historical rows already stored `unknown` for a `reject` verdict,
        # so returning "unknown" here is the report-compatible behavior.
        self._check("reject")

    def test_outcome_kickback(self):
        self._check("kickback")

    def test_table_is_fully_covered(self):
        for verdict_value in OUTCOME_CASES:
            with self.subTest(verdict=verdict_value):
                self.assertTrue(hasattr(self, f"test_outcome_{verdict_value}"),
                                f"outcome row {verdict_value!r} has no method")


class VerdictVocabularyTest(unittest.TestCase):
    """The parser stops at the token; the enum decides whether we know it.

    These cases exist so nobody "fixes" `VERDICT: kick_out` in the extractor:
    the extractor's job ends at the token, and the enum decides what is real.
    """

    def test_unsupported_token_is_not_a_verdict(self):
        self.assertIsNone(Verdict.parse("kick_out"))

    def test_out_of_vocabulary_token_becomes_unknown(self):
        # This is the composition `session.run` performs after the parse; it is
        # asserted here so the semantics are pinned without a live session.
        raw = _extract_verdict("VERDICT: kick_out")
        self.assertIs(Verdict.parse(raw) or Verdict.UNKNOWN, Verdict.UNKNOWN)

    @unittest.expectedFailure
    def test_map_verdict_rejects_out_of_vocabulary_token(self):
        # T20 owns this helper (`harness/core/session.py::_map_verdict`, see
        # plan-2026-08-26-done/T20-unknown-vs-crash.md "Do" #2). The tree maps
        # crashed/parsed inline inside `SessionRunner.run` instead, so there is
        # nothing pure to call and this case fails until T20's helper lands.
        from harness.core.session import _map_verdict

        self.assertIs(_map_verdict(False, "kick_out"), Verdict.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
