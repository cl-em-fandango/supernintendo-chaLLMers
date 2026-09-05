"""Slice 1: the FR-9 `demo` config section (`harness/core/config.py`).

The `demo` section is a typed surface (`harness/core/demo_config.py`): every
key has a default, a config file without the section still loads and answers
every accessor, and the feature is off by default so existing configs keep
working (FR-9). The accessors are thin reads over `Config.raw`; the defaults
themselves live in `harness/core/demo_config.py` and this test asserts the
wiring, not the constants twice.

Run from the repo root:  python3 -m unittest tests.test_demo_config
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config, load
from harness.core.demo_config import (
    DEFAULT_APPS_DIR,
    DEFAULT_CONTENT_MODEL,
    DEFAULT_DEMO_ENABLED,
    DEFAULT_DEPLOY_BRANCH,
    DEFAULT_DEPLOY_DIR_NAME,
    DEFAULT_DOCS_DIR,
    DEFAULT_FALLBACK_TOPIC,
    DemoConfig,
)

WORK_DIR = "/tmp/unused-by-this-test"


def _load(raw: dict) -> Config:
    """`load()` a config written verbatim from `raw` into a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(raw))
        return load(path)


class DemoConfigDefaultsTest(unittest.TestCase):
    """A config with no `demo` section still answers every accessor."""

    def setUp(self):
        self.cfg = _load({"harnessExecutionAndQueueDir": WORK_DIR})

    def test_missing_section_falls_back_to_spec_defaults(self):
        demo = self.cfg.demo
        self.assertIsInstance(demo, DemoConfig)
        self.assertIs(demo.enabled, DEFAULT_DEMO_ENABLED)
        self.assertIs(demo.enabled, False)
        self.assertEqual(demo.deploy_branch, DEFAULT_DEPLOY_BRANCH)
        self.assertEqual(demo.deploy_branch, "pi/app-demo")
        self.assertEqual(demo.apps_dir, DEFAULT_APPS_DIR)
        self.assertEqual(demo.apps_dir, "demo-apps")
        self.assertEqual(demo.docs_dir, DEFAULT_DOCS_DIR)
        self.assertEqual(demo.docs_dir, "docs")
        self.assertEqual(demo.content_model, DEFAULT_CONTENT_MODEL)
        self.assertEqual(demo.content_model, "GLM4.5-AIR_Q4_K_M")
        self.assertEqual(demo.fallback_topic, DEFAULT_FALLBACK_TOPIC)
        self.assertEqual(demo.fallback_topic, "History of Morris Dancing")

    def test_deploy_dir_defaults_under_work_dir(self):
        self.assertEqual(self.cfg.demo.deploy_dir,
                         Path(WORK_DIR) / DEFAULT_DEPLOY_DIR_NAME)

    def test_deploy_dir_defaults_under_harness_exec_dir(self):
        cfg = _load({"harnessExecutionAndQueueDir": WORK_DIR})
        self.assertEqual(cfg.demo.deploy_dir,
                         Path(WORK_DIR) / DEFAULT_DEPLOY_DIR_NAME)
        self.assertEqual(self.cfg.demo.deploy_dir,
                         Path(WORK_DIR) / "demo-deploy")

    def test_empty_section_also_yields_defaults(self):
        cfg = _load({"harnessExecutionAndQueueDir": WORK_DIR, "demo": {}})
        self.assertIs(cfg.demo.enabled, False)
        self.assertEqual(cfg.demo.deploy_branch, "pi/app-demo")
        self.assertEqual(cfg.demo.deploy_dir,
                         Path(WORK_DIR) / "demo-deploy")


class DemoConfigOverrideTest(unittest.TestCase):
    """Present keys win over the defaults."""

    def setUp(self):
        self.cfg = _load({
            "harnessExecutionAndQueueDir": WORK_DIR,
            "demo": {
                "enabled": True,
                "deployBranch": "pi/pages",
                "appsDir": "apps",
                "docsDir": "public",
                "contentModel": "some-other-model",
                "fallbackTopic": "Potted History of Screws",
                "deployDir": "/tmp/demo-checkout",
            },
        })

    def test_configured_values_win(self):
        demo = self.cfg.demo
        self.assertIs(demo.enabled, True)
        self.assertEqual(demo.deploy_branch, "pi/pages")
        self.assertEqual(demo.apps_dir, "apps")
        self.assertEqual(demo.docs_dir, "public")
        self.assertEqual(demo.content_model, "some-other-model")
        self.assertEqual(demo.fallback_topic, "Potted History of Screws")
        self.assertEqual(demo.deploy_dir, Path("/tmp/demo-checkout"))

    def test_enabled_parses_explicit_false(self):
        cfg = _load({"harnessExecutionAndQueueDir": WORK_DIR, "demo": {"enabled": False}})
        self.assertIs(cfg.demo.enabled, False)

    def test_enabled_true_enables_the_feature(self):
        cfg = _load({"harnessExecutionAndQueueDir": WORK_DIR, "demo": {"enabled": True}})
        self.assertIs(cfg.demo.enabled, True)
        # untouched keys keep their defaults alongside an explicit enable
        self.assertEqual(cfg.demo.deploy_branch, "pi/app-demo")


class ShippedConfigTest(unittest.TestCase):
    """The repo's own `config.json` spells the FR-9 `demo` block off."""

    def test_shipped_config_carries_the_demo_block(self):
        # The block must exist and parse; `enabled` and `deployDir` are
        # operator choices (the feature is on in the live deployment) and
        # are deliberately not pinned here.
        cfg = load(Path(__file__).resolve().parent.parent / "config.json")
        demo = cfg.demo
        self.assertIsInstance(demo.enabled, bool)
        self.assertIsInstance(demo.deploy_dir, Path)
        self.assertEqual(demo.deploy_branch, "pi/app-demo")
        self.assertEqual(demo.apps_dir, "demo-apps")
        self.assertEqual(demo.docs_dir, "docs")
        self.assertEqual(demo.content_model, "GLM4.5-AIR_Q4_K_M")
        self.assertEqual(demo.fallback_topic, "History of Morris Dancing")


if __name__ == "__main__":
    unittest.main()
