# T19 — Parse the verdict case-insensitively, from assistant text only

**Wave 4** · depends: T17 · finding: F5

## Context
`_extract_verdict` (end of `external/pi_cli.py`) uses `VERDICT:\s*([a-z_]+)` with a JSON fallback
`"verdict"\s*:\s*"([a-z_]+)"`. Two bugs: an assistant that writes `VERDICT: DONE` (which models do
constantly) matches nothing and returns `"unknown"`, and stages treat `unknown` as failure — so a
perfectly good session drives a retry and eventually a park. Second, before T17 the stderr text was
spliced into `output`, so stderr could fabricate a verdict; T17 removed the splice, and this card
locks the boundary with tests and a guard so it cannot come back.

## Read first
- `external/pi_cli.py` — `_extract_verdict` and its two call sites in `run_pi_session`; the
  `message_end` / `agent_end` parsing that builds `output`; `PiSessionResult`
- `harness/core/session.py` — `verdict = _extract_verdict(result.output)` and the
  `if result.crashed and verdict == "unknown"` line (T20 owns that line, do not edit it here)
- `harness/core/enums.py` — `Verdict` values are all lowercase; the parsed group must be lowercased
  to land on an enum value

## Do
1. `VERDICT_RE = re.compile(r"VERDICT\s*:\s*([A-Za-z_]+)", re.IGNORECASE)`; on a match return
   `m.group(1).strip().lower()` — the enum vocabulary is lowercase, the *wire* may be any case.
2. Same for the JSON fallback: `"verdict"\s*:\s*"([A-Za-z_]+)"` with `re.IGNORECASE`, lowercasing the
   group. Try the plain `VERDICT:` line first, JSON second, `"unknown"` last.
3. Keep the function pure: `_extract_verdict(text: str) -> str`, no I/O, no config import.
4. Guard the stderr boundary **without** re-reading stderr: `_extract_verdict` takes one string and
   the call site passes `result.output`. Add a one-line comment at the call site naming T17 as the
   reason `output` is assistant text only. Do not add a `stderr` parameter.
4b. If the same run yields several verdict lines, the **last** one wins (a session that changes its
   mind after re-checking is normal); use `findall` and take `[-1]`, and say so in the docstring.
5. Do not touch `_outcome` in `session.py` and do not add enum members (T20, T28 own those).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys; sys.path.insert(0,'.')
from external.pi_cli import _extract_verdict as v
cases = {
    "all good VERDICT: done": "done",
    "all good VERDICT: DONE": "done",            # the bug this card exists for
    "text\nVerdict: Pass\n": "pass",
    "VERDICT: kickback": "kickback",
    'noise {"verdict": "KICKBACK"} more': "kickback",
    "VERDICT: RESLICED at the end": "resliced",
    "no verdict anywhere": "unknown",
    "VERDICT: ": "unknown",
    "first VERDICT: fail then VERDICT: pass": "pass",
    "[stderr]\nVERDICT: pass": "pass",           # bare string in == parseable; the call site is
}                                                # what must never feed it stderr (see T17 + below)
for text, want in cases.items():
    got = v(text)
    assert got == want, f"{text!r} -> {got!r}, want {want!r}"
import subprocess
rc = subprocess.run([sys.executable, "-c", """
import sys, re, pathlib; sys.path.insert(0,'.')
src = pathlib.Path('external/pi_cli.py').read_text()
assert 're.IGNORECASE' in src, 'no case-insensitive parse'
assert '[stderr]' not in src, 'stderr splice is back'
m = re.search(r'_extract_verdict\\(([^)]*)\\)', src.split('def run_pi_session')[1])
assert 'stderr' not in m.group(1).lower(), 'stderr passed into the verdict parser'
print('call-site guard ok')
"""], capture_output=True, text=True)
assert rc.returncode == 0, rc.stderr
print("verdict parse ok")
PY
```
Must pass, plus the Gate.

## Out of scope
The `unknown` / `error` / `no_verdict` distinction (T20), adding `KICKOUT`/`UNKNOWN`/`ERROR` to the
`Verdict` enum (T28), replacing `verdict == "..."` with enum comparisons (T29), any change to what
`pi` prints, and any change to the stderr drain itself (T17).

## Done when
`VERDICT: DONE` parses to `"done"`; the JSON fallback is case-insensitive too; the last verdict line
wins; the source no longer contains `[stderr]` and no `stderr` argument reaches `_extract_verdict`.
