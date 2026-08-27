# T40 — Declare project metadata and advisory ruff policy

**Wave 9** · depends: T31 · leaf ticket

## Context
The repository has no project metadata or lint policy. This card declares current reality only; it does not add CI or enforce lint.

## Read first
- Python imports under `harness/`, `external/`, `harness.py`, `supervisor.py`, and `tests/`
- `CODING_STANDARDS.md`

## Do
1. Confirm whether runtime code has third-party imports.
2. Add `pyproject.toml` with project name `harness`, `requires-python = ">=3.10"`, and the observed dependency list.
3. Add optional dev dependency `ruff` and narrow advisory Ruff `F` rules.
4. Do not change source to satisfy Ruff; record residual findings in the commit message.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import pathlib, tomllib
d=tomllib.loads(pathlib.Path('pyproject.toml').read_text())
assert d['project']['name']=='harness'
assert d['project']['requires-python']=='>=3.10'
assert 'ruff' in d['project']['optional-dependencies']['dev']
assert 'ruff' in d['tool']
print('project metadata ok')
PY
```
Global Gate must pass.

## Out of scope
Gate script (T69), README (T59), CI/CD, hooks, formatting, source cleanup, package publishing.

## Done when
`pyproject.toml` accurately declares runtime dependencies and an advisory Ruff policy, with no source or documentation edits.
