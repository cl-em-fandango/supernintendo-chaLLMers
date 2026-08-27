# T34 — Table tests for verdict extraction (no subprocess)

**Wave 9** · depends: T19, T20 · finding: F11

## Context
`external/pi_cli.py` is the most crash-prone module in the repo and has **zero** tests — the whole
boundary layer (pi, git, handlers, supervisor, stats analytics) is untested (F11). The verdict parser
is the cheapest to pin: it is pure text in, string out, and it silently drove real retry/park loops
(`unknown` = 21 of the 56 historical rows). This card is the first of six boundary-test cards; it adds
no fixtures, no fakes and no subprocess — that is T35's job.

## Read first
- `external/pi_cli.py` — `_extract_verdict` and the JSON-stream parsing (`message_end`, `agent_end`)
- `harness/core/session.py` — `_outcome`, and `_map_verdict` if T20 landed
- `tests/test_checkpoint_state.py` — the house style: plain `unittest`, `tmp_path`-equivalent
  `tempfile.mkdtemp`, no pytest, no third-party imports

## Do
1. New file `tests/test_pi_verdict.py`, `unittest.TestCase`, no subprocess and no filesystem writes.
2. Table-drive it from a list of `(text, expected)` pairs. Minimum coverage:
   exact-lowercase (`VERDICT: done`), all-caps (`VERDICT: DONE`), mixed (`Verdict: Pass`), trailing
   prose (`VERDICT: pass — all good`), JSON fallback (`{"verdict": "kickback"}`), JSON-in-prose,
   multiple verdict lines (last wins), no verdict → `unknown`, `VERDICT:` with nothing after it →
   `unknown`, verdict with digits/underscore (`VERDICT: kick_out` → `unknown` at the parse layer if
   `kick_out` is outside the vocabulary — assert whatever the code does, do not change the code),
   empty string, and a 10 KB text with the verdict on the last line.
3. Add `_outcome` cases for every verdict value present in the historical data
   (`pass done unknown error reject kickback`) asserting the outcome string is unchanged — that is
   the report-compatibility test, and it belongs here because it is pure.
4. If a case reveals a parser bug, **do not fix the parser in this card**: write the test as
   `@unittest.expectedFailure` with a comment naming the card that owns the fix (T19/T20) and list it
   in the commit message.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess, unittest, pathlib; sys.path.insert(0,'.')
p = pathlib.Path('tests/test_pi_verdict.py')
assert p.exists(), "missing tests/test_pi_verdict.py"
src = p.read_text()
assert 'subprocess' not in src, "T34 must not spawn processes"
suite = unittest.defaultTestLoader.loadTestsFromName('tests.test_pi_verdict')
n = suite.countTestCases()
assert n >= 14, f"only {n} cases, expected a table of >= 14"
r = unittest.TextTestRunner(verbosity=0).run(suite)
assert r.wasSuccessful(), r.failures + r.errors
print(f"verdict tests ok ({n} cases)")
PY
```
Must pass, plus the Gate (test count must go **up** by this card's cases and failures stay 0).

## Out of scope
Anything that spawns a process — stderr drain, watchdog, crash and rc handling are **T35**. Also out:
changing `_extract_verdict`, `_outcome` or `_map_verdict` (this card tests, it does not fix), git,
handlers, stats report rendering (T39), and adding a pytest dependency or a `conftest.py`.

## Done when
`tests/test_pi_verdict.py` exists with ≥14 cases and passes; `grep -c subprocess tests/test_pi_verdict.py`
is 0; every expected failure is annotated with the owning card; the Gate's test count increased.
