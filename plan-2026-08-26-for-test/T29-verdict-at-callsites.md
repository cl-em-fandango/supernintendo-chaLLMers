# T29 — Compare verdicts as enum members, not as string literals

**Wave 7** · depends: T28 · finding: F9

## Context
`harness/workflow/pipeline.py` has 12 raw `verdict == "..."` comparisons and `Verdict` has zero call
sites — the enum exists for decoration. CODING_STANDARDS.md requires discrete state to be an enum
member inside our code with raw strings only at process edges. The edge here is `_extract_verdict`
(parses the model's `VERDICT:` line) and the stats row (`verdict` is written as a string). Everything
between those two points should be `Verdict.*`. This is a mechanical card and its only risk is
accidentally changing a value, so the verify block is a diff audit, not a behaviour test.

## Read first
- `harness/core/enums.py` — the corrected `Verdict` from T28 (including `KICKOUT`, `UNKNOWN`,
  `ERROR`, and `NO_VERDICT` if T20 landed)
- `harness/workflow/pipeline.py` — every `verdict` mention; `stage_feasibility` (compares
  `"kickout"`), the review loops, `stage_holistic` (`pass` → merge)
- `harness/core/session.py` — `run()`'s return value: what type does the pipeline actually receive
  after T20? Match it.
- `harness/core/stats.py` — where the verdict string is written into the JSONL row

## Do
1. Establish the type at the boundary first and write it down in the commit message: after T20,
   `SessionRunner.run()` hands the pipeline a `Verdict` (preferred). If it still hands back a `str`,
   convert once, at the top of `_run`, with `Verdict.parse(...) or Verdict.UNKNOWN` — one conversion,
   not twelve.
2. Replace all 12 `verdict == "<literal>"` with `verdict is Verdict.<MEMBER>`. Use `is`, not `==`
   (members are singletons; `==` on a `str`-Enum silently accepts raw strings and defeats the point).
3. Keep `_outcome(verdict)` returning a `str` — it feeds the stats row (`outcome` column, wire value)
   — and call `.value` only at that write, never inside a comparison.
4. If a comparison exists against a value **not** in `Verdict` (e.g. a typo like `"kick_out"`), do
   not silently add a member: STOP and record it in the handover, because it means a code path can
   never match.
5. Do not touch `Stage` strings (T30), do not touch `_outcome`'s mapping table (T20).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, re, pathlib; sys.path.insert(0,'.')
src = pathlib.Path('harness/workflow/pipeline.py').read_text()
left = re.findall(r'verdict\s*[!=]=\s*["\'][a-z_]+["\']', src)
assert not left, f"raw verdict comparisons remain: {left}"
assert 'Verdict.' in src, "no enum comparisons introduced"
assert 'verdict ==' not in src and 'verdict !=' not in src
assert len(re.findall(r'verdict is Verdict\.', src)) >= 10, "suspiciously few replacements"
# the wire value written to stats must still be a plain string
import subprocess
rc = subprocess.run([sys.executable,"-c","""
import sys, json, tempfile, pathlib; sys.path.insert(0,'.')
from harness.core.enums import Verdict
from harness.core import session as S
assert isinstance(S._outcome('pass'), str)
assert S._outcome('pass') == 'pass'
print('wire ok')"""], capture_output=True, text=True)
assert rc.returncode == 0, rc.stderr
print("verdict call sites ok")
PY
python3 -m unittest discover -s tests      # existing resume/pipeline tests are the behaviour net
```
Must pass, plus the Gate. Put the `grep -c 'verdict == "'` before/after counts in the commit message.

## Out of scope
The stage-name literals (T30 owns them, including the f-string `f"{kind}_review"`), the verdict
*parsing* regex (T19), `no_verdict` / `error` semantics (T20), adding enum members (T28 — if one is
missing, this card stops rather than edits `enums.py`), `_outcome`'s bucket table, and the two
`_review_loop` behaviour bugs (T41: wrong model for functional fixes, note-path collision).

## Done when
`grep -n 'verdict == "' harness/workflow/pipeline.py` prints nothing; every comparison is
`is Verdict.<X>`; the stats row's `verdict`/`outcome` fields are still plain strings identical to
before; the 40 existing tests still pass.
