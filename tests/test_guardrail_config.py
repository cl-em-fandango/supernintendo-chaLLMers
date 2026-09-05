"""Slice 4.1: the FR-4 guardrail config keys (`harness/core/config.py`).

Every limit the hardened subprocess layer needs is a `config.json` key with a
default, and a missing key must fall back to that default without erroring
(spec FR-4.4). The accessors are thin reads over `Config.raw`; the numbers
themselves are owned by the enforcing modules
(`external/hardened_process.py`, `external/bash_ulimit.py`) and this test
asserts the wiring, not the constants twice.

Run from the repo root:  python3 -m unittest tests.test_guardrail_config
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import (
    DEFAULT_SESSION_TIMEOUT_S,
    Config,
    load,
)
from external.bash_ulimit import DEFAULT_ULIMIT_NPROC, DEFAULT_ULIMIT_VMEM_KB
from external.hardened_process import (
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_TOOL_TIMEOUT_S,
    GuardrailLimits,
)


def _load(raw: dict) -> Config:
    """`load()` a config written verbatim from `raw` into a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(raw))
        return load(path)


class GuardrailConfigDefaultsTest(unittest.TestCase):
    """A config without the new keys still answers every guardrail accessor."""

    def setUp(self):
        self.cfg = _load({"harnessExecutionAndQueueDir": "/tmp/unused-by-this-test"})

    def test_missing_keys_fall_back_to_spec_defaults(self):
        self.assertEqual(self.cfg.session_timeout, DEFAULT_SESSION_TIMEOUT_S)
        self.assertEqual(self.cfg.session_timeout, 3600)
        self.assertEqual(self.cfg.tool_timeout, DEFAULT_TOOL_TIMEOUT_S)
        self.assertEqual(self.cfg.tool_timeout, 60)
        self.assertEqual(self.cfg.max_output_bytes, DEFAULT_MAX_OUTPUT_BYTES)
        self.assertEqual(self.cfg.max_output_bytes, 2_097_152)
        self.assertEqual(self.cfg.tool_ulimit_nproc, DEFAULT_ULIMIT_NPROC)
        self.assertEqual(self.cfg.tool_ulimit_nproc, 50)
        self.assertEqual(self.cfg.tool_ulimit_vmem_kb, DEFAULT_ULIMIT_VMEM_KB)
        self.assertEqual(self.cfg.tool_ulimit_vmem_kb, 8_388_608)

    def test_guardrail_limits_object_carries_the_defaults(self):
        limits = self.cfg.guardrail_limits()
        self.assertIsInstance(limits, GuardrailLimits)
        self.assertEqual(limits.timeout_s, 60)
        self.assertEqual(limits.max_output_bytes, 2_097_152)
        self.assertEqual(limits.ulimit_nproc, 50)
        self.assertEqual(limits.ulimit_vmem_kb, 8_388_608)


class GuardrailConfigOverrideTest(unittest.TestCase):
    """Present keys win, and `guardrail_limits()` reflects them."""

    def setUp(self):
        self.cfg = _load({
            "harnessExecutionAndQueueDir": "/tmp/unused-by-this-test",
            "sessionTimeout": 7200,
            "toolTimeout": 30,
            "maxOutputBytes": 4096,
            "toolUlimitNproc": 64,
            "toolUlimitVmemKB": 1048576,
        })

    def test_configured_values_win(self):
        self.assertEqual(self.cfg.session_timeout, 7200)
        self.assertEqual(self.cfg.tool_timeout, 30)
        self.assertEqual(self.cfg.max_output_bytes, 4096)
        self.assertEqual(self.cfg.tool_ulimit_nproc, 64)
        self.assertEqual(self.cfg.tool_ulimit_vmem_kb, 1048576)

    def test_limits_object_reflects_configured_values(self):
        limits = self.cfg.guardrail_limits()
        self.assertEqual(limits.timeout_s, 30)
        self.assertEqual(limits.max_output_bytes, 4096)
        self.assertEqual(limits.ulimit_nproc, 64)
        self.assertEqual(limits.ulimit_vmem_kb, 1048576)


class ShippedConfigTest(unittest.TestCase):
    """The repo's own `config.json` spells the §5 keys with the §5 values."""

    def test_shipped_config_carries_the_new_keys(self):
        cfg = load(Path(__file__).resolve().parent.parent / "config.json")
        self.assertEqual(cfg.session_timeout, 3600)
        self.assertEqual(cfg.tool_timeout, 60)
        self.assertEqual(cfg.max_output_bytes, 2_097_152)
        self.assertEqual(cfg.tool_ulimit_nproc, 50)
        self.assertEqual(cfg.tool_ulimit_vmem_kb, 8_388_608)


if __name__ == "__main__":
    unittest.main()
