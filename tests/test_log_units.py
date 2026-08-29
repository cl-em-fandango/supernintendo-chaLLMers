"""T33: truthful log units and an explicit retry default (findings F10, F14).

`session.py` used to print `budget=60000k ctx=131072k`: a `k` suffix on an
*unscaled* integer, so the line read as 60 million and 131 million tokens.
Nothing consumed that line programmatically, so no test ever caught it — an
operator reading a real 60000-token budget as 60M either trusts a wrong number
or wastes a cycle on the discrepancy.

These tests pin the wire contract of the session-start line, plus the config
keys the ticket makes explicit:
- the line reads `budget=<n> tokens ctx=<n> tokens`, the numbers equal to
  `Config.model_budget()` / `Config.model_context()`, with no `k` suffix;
- `config.json` states `maxCrashRetries` (the previously invisible default of
  2, unchanged behavior) and ends with exactly one newline.

Run from the repo root:  python3 -m unittest tests.test_log_units
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core import config as config_module
from harness.core.config import Config, DEFAULT_CONTEXT_WINDOW
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.workflow.pipeline import Pipeline
from external.pi_cli import PiSessionResult

REPO_ROOT = Path(__file__).resolve().parent.parent

# The session-start line, exactly as the operator must see it.
START_LINE = re.compile(
    r"▶ (?P<stage>\w+) model=(?P<model>\S+) iter=(?P<iter>\d+) "
    r"budget=(?P<budget>\d+) tokens ctx=(?P<ctx>\d+) tokens$"
)
# A `k` stuck to a logged count — the bug, in either spelling.
K_SUFFIX = re.compile(r"(budget|ctx)=\d+k")


def _cfg(work_dir: Path, model_context_map: dict | None = None) -> Config:
    return Config(
        work_dir=work_dir,
        token_budget=60_000,
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
        model_context_map=model_context_map or {},
    )


def _fake_pi(output: str):
    """Stand in for `external.pi_cli.run_pi_session`: no subprocess, fixed text."""

    # `max_context_tokens` is the cap T49 hands to the stream layer; this double
    # takes it and ignores it — the cap itself is asserted in
    # tests/test_over_cap_session.py.
    def run(*, model, workdir, prompt, out_file, log,
            max_context_tokens=None) -> PiSessionResult:
        Path(out_file).write_text(output)
        return PiSessionResult(rc=0, crashed=False, err="", peak_tokens=7,
                               duration_s=0.1, output=output,
                               out_file=Path(out_file), stderr="")

    return run


class SessionStartUnitsTest(unittest.TestCase):
    """`SessionRunner.run` logs raw counts with the word `tokens`, never `k`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.lines: list[str] = []

    def _start_line(self, cfg: Config, model: str,
                    stage: Stage = Stage.SLICE_IMPLEMENT,
                    iteration: int = 2) -> str:
        """Run one stubbed session and return its session-start log line."""
        runner = SessionRunner(cfg, StatsStore(cfg.stats_path), log=self.lines.append)
        with patch("harness.core.session.run_pi_session",
                   _fake_pi("## Summary\nworked\n\nVERDICT: done")):
            runner.run(model, self.work_repo, "p", task_id="t1",
                       stage=stage, iteration=iteration)
        starts = [line for line in self.lines if "budget=" in line]
        self.assertEqual(len(starts), 1, f"expected one session-start line: {self.lines}")
        return starts[0].strip()

    def test_start_line_reads_tokens_units(self):
        cfg = _cfg(self.work_dir, {"m": 65_536})
        line = self._start_line(cfg, "m")
        m = START_LINE.search(line)
        self.assertIsNotNone(m, f"session-start line not in expected form: {line!r}")
        self.assertEqual(m.group("stage"), "slice_implement")
        self.assertEqual(m.group("model"), "m")
        self.assertEqual(m.group("iter"), "2")
        self.assertEqual(int(m.group("budget")), cfg.model_budget("m"))
        self.assertEqual(int(m.group("ctx")), cfg.model_context("m"))

    def test_no_k_suffix_on_any_logged_count(self):
        cfg = _cfg(self.work_dir, {"m": 131_072})
        line = self._start_line(cfg, "m")
        self.assertEqual(K_SUFFIX.findall(line), [], f"unscaled count suffixed 'k': {line!r}")
        self.assertIn("budget=60000 tokens", line)
        self.assertIn("ctx=131072 tokens", line)

    def test_defaulted_window_is_logged_as_raw_tokens(self):
        """An unknown model still logs a plain token count (the 128k default)."""
        cfg = _cfg(self.work_dir, {})
        line = self._start_line(cfg, "UnmappedModel")
        self.assertIn(f"ctx={DEFAULT_CONTEXT_WINDOW} tokens", line)
        self.assertEqual(K_SUFFIX.findall(line), [])


class ConfigKeysTest(unittest.TestCase):
    """The shipped config states the crash-retry default and is newline-terminated."""

    def setUp(self):
        self.raw_text = (REPO_ROOT / "config.json").read_text()
        self.raw = json.loads(self.raw_text)

    def test_max_crash_retries_is_explicit(self):
        self.assertEqual(self.raw.get("maxCrashRetries"), 2)

    def test_declared_value_matches_the_code_default(self):
        """Declaring the key must not change behavior: the code default is 2."""
        cfg = config_module.load(REPO_ROOT / "config.json")
        self.assertEqual(cfg.get("maxCrashRetries", 2),
                         Pipeline(cfg, runner=None, log=lambda *a: None)
                         .max_crash_retries)

    def test_file_ends_with_exactly_one_newline(self):
        self.assertTrue(self.raw_text.endswith("\n"), "config.json missing newline")
        self.assertFalse(self.raw_text.endswith("\n\n"), "config.json has a trailing blank line")


if __name__ == "__main__":
    unittest.main()
