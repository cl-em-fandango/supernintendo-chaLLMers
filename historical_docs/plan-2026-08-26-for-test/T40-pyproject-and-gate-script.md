# T40 — Declare project metadata and advisory ruff policy

**Wave 9** · depends: T31 · leaf ticket

## Context
The repository has no project metadata or lint policy. This card declares current reality only; it does not add CI or enforce lint.

## Read first
- `CODING_STANDARDS.md`

No module sweep: the runtime import set is discovered by the command in `Do` step 1, so this card's
read set stays inside the hard limit instead of reading every Python file.

## Do
1. Discover the runtime import set with
   `grep -rhoE --include='*.py' '^[[:space:]]*(import|from) [a-zA-Z0-9_.]+' harness external harness.py supervisor.py | sort -u`
   and treat anything that is neither the standard library nor a local module as a runtime dependency.
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
