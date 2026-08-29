# T43 — Specification assessment must fail closed

**Wave 7** · depends: T20, T28, T29 · finding: hardening review G1/G2/G3

## Context
`Pipeline.stage_spec()` currently treats only `KICKBACK` specially. Every other assessor result — including `ERROR`, `UNKNOWN`, `NO_VERDICT`, `FAIL`, or an unsupported verdict — falls through to `spec approved`. Verdict parsing and enum adoption do not fix this routing defect.

## Read first
- `harness/workflow/pipeline.py` — `stage_spec`, `_run`
- `harness/core/session.py` — `SessionResult`, verdict mapping
- `harness/core/enums.py` — `Verdict`
- existing pipeline test style in `tests/test_pipeline_resume.py`

## Do
1. Define the accepted assessor protocol explicitly:
   - `Verdict.PASS` means that assessor approved the specification;
   - `Verdict.KICKBACK` follows the existing revision loop;
   - every other verdict parks the task with assessor name and verdict in the reason.
2. Apply the same rule independently to Ornith and the technical-writer requirement check.
3. Do not reinterpret process failure as a content verdict. If `SessionResult.ok` is false, park with a process-failure reason even if partial output contains `PASS`.
4. Preserve the existing kickback counter and maximum exactly.
5. Add `tests/test_spec_assessment_routing.py` using a stub runner. Cover both assessors and prove that `ERROR`, `NO_VERDICT`, `UNKNOWN`, and `FAIL` cannot reach `spec approved`.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_spec_assessment_routing -v
python3 - <<'PY'
from pathlib import Path
src = Path('harness/workflow/pipeline.py').read_text()
assert 'Verdict.PASS' in src
assert 'spec approved' in src
print('spec assessment routing present')
PY
```
Gate must pass.

## Out of scope
Verdict parsing (T19), enum vocabulary (T28), feasibility/review routing, assessor retry policy beyond the existing kickback loop, prompt changes.

## Done when
Both assessors require an explicit healthy `PASS`; all other non-kickback results park; the dedicated tests prove no error or unsupported verdict can approve a specification.
