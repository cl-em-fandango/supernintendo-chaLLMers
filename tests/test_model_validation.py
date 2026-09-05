"""Tests for model validation via pi --list-models before process execution."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import external.pi_cli as P
from harness.core.config import Config, load
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.cli import handlers


def _make_fake_pi(script_body: str, bin_dir: Path) -> None:
    body = textwrap.indent(textwrap.dedent(script_body).strip("\n"), "    ")
    (bin_dir / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "try:\n"
        f"{body}\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (bin_dir / "pi").chmod(0o755)


class TestModelParsing(unittest.TestCase):
    def test_parse_plain_lines(self):
        output = "model-alpha\nmodel-beta\nmodel-gamma\n"
        parsed = P.parse_model_list_output(output)
        self.assertIn("model-alpha", parsed)
        self.assertIn("model-beta", parsed)
        self.assertIn("model-gamma", parsed)

    def test_parse_bullet_lines(self):
        output = "- model-one\n* model-two\n+ model-three\n"
        parsed = P.parse_model_list_output(output)
        self.assertIn("model-one", parsed)
        self.assertIn("model-two", parsed)
        self.assertIn("model-three", parsed)

    def test_parse_provider_prefixes(self):
        output = "llama-swap/Ornith-1.5-35B-Q6_K\nopenai/gpt-4\n"
        parsed = P.parse_model_list_output(output)
        self.assertIn("llama-swap/Ornith-1.5-35B-Q6_K", parsed)
        self.assertIn("Ornith-1.5-35B-Q6_K", parsed)
        self.assertIn("openai/gpt-4", parsed)
        self.assertIn("gpt-4", parsed)

    def test_parse_json_array(self):
        output = '[{"id": "model-1", "name": "Model One"}, {"id": "llama-swap/model-2"}]'
        parsed = P.parse_model_list_output(output)
        self.assertIn("model-1", parsed)
        self.assertIn("Model One", parsed)
        self.assertIn("llama-swap/model-2", parsed)

    def test_parse_json_dict_with_models(self):
        output = '{"models": [{"id": "model-a"}, {"name": "model-b"}]}'
        parsed = P.parse_model_list_output(output)
        self.assertIn("model-a", parsed)
        self.assertIn("model-b", parsed)

    def test_parse_empty_output(self):
        self.assertEqual(P.parse_model_list_output(""), [])
        self.assertEqual(P.parse_model_list_output("   \n\n  "), [])


class TestModelValidationSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.bin_dir = self.tmp_path / "bin"
        self.bin_dir.mkdir()
        self.orig_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}:{self.orig_path}"

    def tearDown(self):
        os.environ["PATH"] = self.orig_path
        self.tmp.cleanup()

    def test_list_available_pi_models_success(self):
        _make_fake_pi("""
            if "--list-models" in sys.argv:
                print("model-alpha")
                print("llama-swap/model-beta")
                sys.exit(0)
            sys.exit(1)
        """, self.bin_dir)

        models = P.list_available_pi_models()
        self.assertIn("model-alpha", models)
        self.assertIn("model-beta", models)

    def test_list_available_pi_models_failure(self):
        _make_fake_pi("""
            sys.stderr.write("backend connection refused\\n")
            sys.exit(2)
        """, self.bin_dir)

        with self.assertRaises(RuntimeError) as ctx:
            P.list_available_pi_models()
        self.assertIn("backend connection refused", str(ctx.exception))

    def test_validate_models_present_all_available(self):
        _make_fake_pi("""
            print("model-alpha")
            print("llama-swap/model-beta")
            print("model-gamma.gguf")
        """, self.bin_dir)

        # Should not raise
        P.validate_models_present(["model-alpha", "model-beta", "model-gamma"])

    def test_validate_models_present_missing_raises(self):
        _make_fake_pi("""
            print("model-alpha")
        """, self.bin_dir)

        with self.assertRaises(RuntimeError) as ctx:
            P.validate_models_present(["model-alpha", "missing-model-x"])
        self.assertIn("missing-model-x", str(ctx.exception))


class TestConfiguredModels(unittest.TestCase):
    def test_config_configured_models_extracts_all_roles_and_pools(self):
        cfg = Config(
            harness_execution_and_queue_dir=Path("/tmp/work"),
            token_budget=60000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=5,
            trunk_branch="pi/trunk",
            task_provider="directory",
            directory_provider={},
            models={
                "assessor": "model-assessor",
                "implementer": "model-implementer",
                "technicalWriter": "model-writer",
                "fastPool": ["model-fast-1", "model-fast-2"],
                "randomPool": ["model-fast-1", "model-random-3"],
            },
            model_context_map={},
        )
        models = cfg.configured_models
        self.assertIn("model-assessor", models)
        self.assertIn("model-implementer", models)
        self.assertIn("model-writer", models)
        self.assertIn("model-fast-1", models)
        self.assertIn("model-fast-2", models)
        self.assertIn("model-random-3", models)
        self.assertEqual(len(models), len(set(models)))


class TestSessionRunnerValidation(unittest.TestCase):
    def test_session_runner_validates_models_before_run(self):
        cfg = Config(
            harness_execution_and_queue_dir=Path("/tmp/work"),
            token_budget=60000,
            max_spec_kickbacks=3,
            max_slice_implement=5,
            max_slice_tech_review=5,
            max_slice_func_review=5,
            max_slice_check_loops=3,
            autonomous_queue_target=5,
            trunk_branch="pi/trunk",
            task_provider="directory",
            directory_provider={},
            models={
                "assessor": "model-1",
                "implementer": "model-2",
                "technicalWriter": "model-3",
            },
            model_context_map={},
        )
        store = mock.MagicMock()
        runner = SessionRunner(cfg, store, log=lambda *_: None)

        with mock.patch("harness.core.session.validate_models_present") as mock_val, \
             mock.patch("harness.core.session.run_pi_session") as mock_run:
            mock_run.return_value = P.PiSessionResult(
                rc=0, crashed=False, err="", peak_tokens=100, duration_s=1.0,
                output="VERDICT: done", out_file=Path("/tmp/out"),
            )
            runner.run(model="model-1", workdir=Path("/tmp"), prompt="hello")
            mock_val.assert_called_once_with(["model-1", "model-2", "model-3"], log=runner.log)

            # Subsequent run should not validate again (cached)
            runner.run(model="model-1", workdir=Path("/tmp"), prompt="hello again")
            self.assertEqual(mock_val.call_count, 1)


if __name__ == "__main__":
    unittest.main()
