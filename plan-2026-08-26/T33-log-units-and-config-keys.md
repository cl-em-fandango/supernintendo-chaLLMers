# T33 — Fix token log units and declare crash retries

**Wave 8** · depends: T32 · leaf ticket

## Context
`session.py` prints raw token counts with a `k` suffix, and `maxCrashRetries` is an invisible default.

## Read first
- `harness/core/session.py` — session-start log line
- `harness/core/config.py` — config access
- `config.json`

## Do
1. Log raw counts as `budget=<n> tokens ctx=<n> tokens`; never append `k` to an unscaled integer.
2. Add `"maxCrashRetries": 2` to `config.json`, preserving behavior.
3. Ensure `config.json` ends with one newline.
4. Add `tests/test_log_units.py` asserting the session-start line reads `budget=<n> tokens
   ctx=<n> tokens` for a stubbed `run_pi_session`. No existing test executes that line, so the new
   module is unconditional.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import json, pathlib, re
s=pathlib.Path('harness/core/session.py').read_text()
assert not re.search(r'(budget|ctx)=\{[^}]+\}k', s)
r=pathlib.Path('config.json').read_text()
assert json.loads(r)['maxCrashRetries'] == 2 and r.endswith('\n')
print('units/config ok')
PY
python3 -m unittest tests.test_log_units -v
```
Global Gate must pass.

## Out of scope
README and historical docs (T59), budget arithmetic (T32), over-cap enforcement (T48, T49, T74, T75), retry behavior changes.

## Done when
Logs use truthful units, the existing retry default is explicit, config has a newline, and no documentation changed.
