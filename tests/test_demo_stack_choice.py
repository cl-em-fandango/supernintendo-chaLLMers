"""Slice 7.1 — stack selection + Pages base-path helpers (FR-3.2, FR-7.5).

Pure unit tests, no pi, no npm, no git: the ticket text decides the
stack (explicit request wins, unspecified falls to CRA + Material UI),
and the stack plan carries the build commands, the standard artifact
directory and the Pages project-site subpath derived from the repo name.

Run from the repo root:
    python3 -m unittest tests.test_demo_stack_choice
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.workflow.demo_stack import (
    DEFAULT_STACK,
    WebStack,
    build_stack_plan,
    detect_stack,
    pages_base_path,
    repo_name,
)


class DetectStackTest(unittest.TestCase):
    def test_unspecified_ticket_gets_the_default_stack(self):
        self.assertEqual(detect_stack("Make me a site about pizza"),
                         DEFAULT_STACK)
        self.assertEqual(DEFAULT_STACK, WebStack.CRA_MUI)

    def test_plain_html_request_wins(self):
        for text in ("plain HTML please", "just static HTML",
                     "vanilla html only", "no framework, no build"):
            self.assertEqual(detect_stack(text), WebStack.PLAIN_HTML, text)

    def test_vue_request_wins(self):
        self.assertEqual(detect_stack("Build this with Vue"), WebStack.VUE)
        self.assertEqual(detect_stack("use vue.js"), WebStack.VUE)

    def test_react_or_mui_request_names_the_default_explicitly(self):
        self.assertEqual(detect_stack("use React"), WebStack.CRA_MUI)
        self.assertEqual(detect_stack("create-react-app with Material UI"),
                         WebStack.CRA_MUI)

    def test_substring_lookalikes_do_not_trigger_vue(self):
        # "value" contains "vue"; word boundaries keep it out.
        self.assertEqual(
            detect_stack("a site celebrating the value of pizza"),
            WebStack.CRA_MUI)

    def test_empty_text_falls_to_the_default(self):
        self.assertEqual(detect_stack(""), WebStack.CRA_MUI)


class PagesBasePathTest(unittest.TestCase):
    def test_base_path_from_owner_repo_slug(self):
        self.assertEqual(pages_base_path("acme/widgets"), "/widgets/")

    def test_slug_variants_strip_url_noise(self):
        self.assertEqual(repo_name("https://github.com/acme/widgets.git"),
                         "widgets")
        self.assertEqual(repo_name("widgets"), "widgets")


class StackPlanTest(unittest.TestCase):
    def test_cra_plan(self):
        plan = build_stack_plan(WebStack.CRA_MUI, "acme/widgets")
        self.assertEqual(plan.build_commands,
                         (("install",), ("run", "build")))
        self.assertEqual(plan.artifact_dir, "build")
        self.assertTrue(plan.needs_build)
        self.assertEqual(plan.public_path, "/widgets/")

    def test_vue_plan_uses_dist(self):
        plan = build_stack_plan(WebStack.VUE, "acme/widgets")
        self.assertEqual(plan.artifact_dir, "dist")
        self.assertTrue(plan.needs_build)
        self.assertEqual(plan.public_path, "/widgets/")

    def test_plain_html_plan_needs_no_build(self):
        plan = build_stack_plan(WebStack.PLAIN_HTML, "acme/widgets")
        self.assertEqual(plan.build_commands, ())
        self.assertFalse(plan.needs_build)


if __name__ == "__main__":
    unittest.main()
