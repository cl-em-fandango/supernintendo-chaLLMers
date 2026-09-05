"""Slice 1 — the disabled-state GitHub sync surface (spec FR-0.1, AC-1).

`harness sync` exists, reads the new GitHub config keys, and is a complete,
silent no-op when GitHub is unconfigured. Nothing here talks to the sync
engine (a later slice owns it): this file owns the config properties, the
label vocabulary, and the disabled-state CLI edge.

Covered here:
  * the four config keys (`githubPat`, `githubRepo`, `githubApiBaseUrl`,
    `githubSyncIntervalS`) read from `Config.raw` with the documented
    defaults, and `github_sync_enabled` is true only with both a PAT and a
    repo (FR-0.1);
  * the label Enums carry exactly the §4 strings, the trigger precedence is
    delete > park > ingest (FR-1.5), and the prefix helpers own the
    "is this label ours / is this a state label" questions (FR-2.4);
  * `cmd_sync` with an empty/absent `githubPat`/`githubRepo` prints
    "github sync disabled", exits 0, and writes nothing to the queue;
  * `cmd_sync` with GitHub configured dispatches into the sync engine with
    a fake API injected and still changes nothing on an empty queue (the
    engine's own behavior is tested in the inbound/outbound slices);
  * no slice-1 file can perform HTTP: no `urllib`, `http.client`, `socket`
    or `requests` import anywhere in the files this slice touched — HTTP
    may only ever appear in `external/github_api.py` (NFR-3).
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.cli import handlers  # noqa: E402
from harness.cli.parser import parse_args  # noqa: E402
from harness.core.config import (  # noqa: E402
    DEFAULT_GITHUB_API_BASE_URL,
    DEFAULT_GITHUB_SYNC_INTERVAL_S,
    load,
)
from harness.core.sync_labels import (  # noqa: E402
    HARNESS_LABEL_PREFIX,
    StateLabel,
    TriggerLabel,
    TRIGGER_PRECEDENCE,
    is_harness_label,
    is_state_label,
    state_for,
    trigger_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The files this slice created or changed. The zero-HTTP assertion is scoped
# to them: `core/health.py` already probes the model server and is outside
# this feature's reach.
SLICE_1_FILES = (
    _REPO_ROOT / "harness" / "core" / "config.py",
    _REPO_ROOT / "harness" / "core" / "sync_labels.py",
    _REPO_ROOT / "harness" / "cli" / "parser.py",
    _REPO_ROOT / "harness" / "cli" / "handlers.py",
    _REPO_ROOT / "harness.py",
)

def _config_from(raw: dict):
    """A real `Config` loaded from a temp config.json carrying `raw`."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"harnessExecutionAndQueueDir": tempfile.mkdtemp(), **raw}, f)
        path = Path(f.name)
    try:
        return load(path)
    finally:
        path.unlink()


class ConfigGithubKeysTest(unittest.TestCase):
    def test_defaults_when_keys_absent(self):
        cfg = _config_from({})
        self.assertEqual("", cfg.github_pat)
        self.assertEqual("", cfg.github_repo)
        self.assertEqual(DEFAULT_GITHUB_API_BASE_URL, cfg.github_api_base_url)
        self.assertEqual("https://api.github.com", cfg.github_api_base_url)
        self.assertEqual(DEFAULT_GITHUB_SYNC_INTERVAL_S,
                         cfg.github_sync_interval_s)
        self.assertEqual(60, cfg.github_sync_interval_s)
        self.assertFalse(cfg.github_sync_enabled)

    def test_configured_values_are_returned(self):
        cfg = _config_from({"githubPat": "ghp_token",
                            "githubRepo": "acme/widgets",
                            "githubApiBaseUrl": "https://ghes.example/api/v3",
                            "githubSyncIntervalS": 15})
        self.assertEqual("ghp_token", cfg.github_pat)
        self.assertEqual("acme/widgets", cfg.github_repo)
        self.assertEqual("https://ghes.example/api/v3", cfg.github_api_base_url)
        self.assertEqual(15, cfg.github_sync_interval_s)
        self.assertTrue(cfg.github_sync_enabled)

    def test_pat_alone_does_not_enable(self):
        cfg = _config_from({"githubPat": "ghp_token"})
        self.assertFalse(cfg.github_sync_enabled)

    def test_repo_alone_does_not_enable(self):
        cfg = _config_from({"githubRepo": "acme/widgets"})
        self.assertFalse(cfg.github_sync_enabled)

    def test_whitespace_only_values_are_treated_as_empty(self):
        cfg = _config_from({"githubPat": "   ", "githubRepo": "  "})
        self.assertEqual("", cfg.github_pat)
        self.assertEqual("", cfg.github_repo)
        self.assertFalse(cfg.github_sync_enabled)

    def test_empty_base_url_falls_back_to_default(self):
        cfg = _config_from({"githubApiBaseUrl": ""})
        self.assertEqual(DEFAULT_GITHUB_API_BASE_URL, cfg.github_api_base_url)


class SyncLabelsTest(unittest.TestCase):
    def test_trigger_label_strings_match_spec(self):
        self.assertEqual({"snes", "snes-demo", "snes-parked", "snes-deleted"},
                         {member.value for member in TriggerLabel})

    def test_state_label_strings_match_spec(self):
        self.assertEqual(
            {"snes-pending", "snes-claimed", "snes-active", "snes-review",
             "snes-parked", "snes-failed", "snes-done"},
            {member.value for member in StateLabel})

    def test_parked_label_is_both_trigger_and_state(self):
        self.assertEqual(TriggerLabel.PARK.value, StateLabel.PARKED.value)

    def test_trigger_precedence_is_delete_park_demo_ingest(self):
        self.assertEqual((TriggerLabel.DELETE, TriggerLabel.PARK,
                          TriggerLabel.DEMO, TriggerLabel.INGEST),
                         TRIGGER_PRECEDENCE)

    def test_prefix_constant(self):
        self.assertEqual("snes-", HARNESS_LABEL_PREFIX)

    def test_parsers_round_trip_values(self):
        for member in TriggerLabel:
            self.assertIs(member, trigger_for(member.value))
        for member in StateLabel:
            self.assertIs(member, state_for(member.value))
        self.assertIsNone(trigger_for("snes-pending"))
        self.assertIsNone(state_for("snes"))
        self.assertIsNone(state_for("bug"))

    def test_is_harness_label_owns_our_family_only(self):
        for member in StateLabel:
            self.assertTrue(is_harness_label(member.value), member.value)
        for member in TriggerLabel:
            self.assertTrue(is_harness_label(member.value), member.value)
        self.assertFalse(is_harness_label("bug"))
        self.assertFalse(is_harness_label(""))
        self.assertFalse(is_harness_label("needs-triage"))

    def test_is_state_label_excludes_the_subscription_marker(self):
        for member in StateLabel:
            self.assertTrue(is_state_label(member.value), member.value)
        self.assertFalse(is_state_label("snes"))
        self.assertFalse(is_state_label("snes-deleted"))


class CmdSyncDisabledTest(unittest.TestCase):
    """`harness sync` end-to-end through the real `build()`, temp work dir."""

    def _run_sync(self, raw: dict) -> tuple[int, str]:
        cfg_raw = {"harnessExecutionAndQueueDir": str(self.work), **raw}
        cfg_path = Path(self.work) / "config.json"
        cfg_path.write_text(json.dumps(cfg_raw))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HARNESS_CONFIG": str(cfg_path)}):
            with contextlib.redirect_stdout(out):
                rc = handlers.cmd_sync()
        return rc, out.getvalue()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

    def _queue_snapshot(self) -> dict[str, list[str]]:
        return {sub: sorted(p.name for p in (self.work / "queue" / sub).iterdir())
                if (self.work / "queue" / sub).exists() else []
                for sub in ("pending", "claimed", "active", "review",
                            "parked", "failed", "done")}

    def test_absent_keys_print_disabled_and_exit_zero(self):
        rc, out = self._run_sync({})
        self.assertEqual(0, rc)
        self.assertIn("github sync disabled", out)

    def test_empty_keys_print_disabled_and_exit_zero(self):
        rc, out = self._run_sync({"githubPat": "", "githubRepo": ""})
        self.assertEqual(0, rc)
        self.assertIn("github sync disabled", out)

    def test_pat_without_repo_prints_disabled(self):
        rc, out = self._run_sync({"githubPat": "ghp_token"})
        self.assertEqual(0, rc)
        self.assertIn("github sync disabled", out)

    def test_disabled_pass_changes_nothing_in_the_queue(self):
        before = self._queue_snapshot()
        rc, _out = self._run_sync({})
        self.assertEqual(0, rc)
        self.assertEqual(before, self._queue_snapshot())

    def test_disabled_message_contains_no_token(self):
        rc, out = self._run_sync({"githubPat": "ghp_secrettoken",
                                  "githubRepo": ""})
        self.assertEqual(0, rc)
        self.assertNotIn("ghp_secrettoken", out)

    def test_enabled_config_runs_a_pass_with_the_injected_api(self):
        """The enabled path dispatches into `sync_pass` (no real HTTP: the
        API client is faked at the composition boundary)."""
        class _NoIssuesApi:
            def __init__(self):
                self.calls = 0

            def list_issues(self, labels=(), state=None):
                self.calls += 1
                return []

        fake = _NoIssuesApi()
        with mock.patch.object(handlers, "build_github_api",
                               return_value=fake):
            rc, out = self._run_sync({"githubPat": "ghp_token",
                                      "githubRepo": "acme/widgets"})
        self.assertEqual(0, rc)
        self.assertNotIn("github sync disabled", out)
        self.assertIn("github sync:", out)
        # One ingest listing (open `snes`) plus the two halt triggers read
        # open and closed (Slice 4): five listings per inbound pass.
        self.assertEqual(5, fake.calls)
        self.assertEqual(self._queue_snapshot(),
                         {sub: [] for sub in ("pending", "claimed", "active",
                                              "review", "parked", "failed",
                                              "done")})


class CliSurfaceTest(unittest.TestCase):
    def test_parser_accepts_sync(self):
        args = parse_args(["sync"])
        self.assertEqual("sync", args.command)


class NoHttpCapabilityTest(unittest.TestCase):
    """NFR-3: HTTP lives only in `external/github_api.py`, which is not here yet."""

    def test_slice_one_files_import_no_http_stacks(self):
        for path in SLICE_1_FILES:
            with self.subTest(file=path.name):
                imported = set()
                for node in ast.walk(ast.parse(path.read_text())):
                    if isinstance(node, ast.Import):
                        imported.update(a.name for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module)
                for module in imported:
                    self.assertFalse(
                        module == "urllib" or module.startswith("urllib.")
                        or module == "socket"
                        or module == "http" or module.startswith("http.")
                        or module in ("requests", "aiohttp", "httplib2"),
                        f"{path.name} imports {module}; HTTP belongs in "
                        "external/github_api.py only")


if __name__ == "__main__":
    unittest.main()
