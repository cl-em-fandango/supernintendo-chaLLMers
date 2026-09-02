"""Slice 7 — scaffold writer (FR-3.2, FR-3.3, FR-4.3, FR-7.5).

The deterministic project skeleton: declared build command, standard
artifact directory, Pages-safe base/public path, the Slice 6 content
baked in as `content.json` and referenced by the app source, and every
write confined to `demo-apps/<app-name>/`. No pi, no npm.

Run from the repo root:
    python3 -m unittest tests.test_demo_scaffold
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.workflow.demo_content import ContentSource, SiteContent
from harness.workflow.demo_scaffold import (
    CRA_DARK_THEME_SNIPPET,
    ScaffoldPathError,
    scaffold_app,
    validate_app_name,
)
from harness.workflow.demo_stack import WebStack, build_stack_plan

PAYLOAD = {
    "title": "Pizza Fan Site",
    "sections": [{"heading": "Slices", "body": "rm -rf /; echo $(pwned)"}],
}


def _content() -> SiteContent:
    return SiteContent(payload=dict(PAYLOAD), source=ContentSource.MODEL)


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apps_dir = Path(self._tmp.name) / "demo-apps"

    def _scaffold(self, stack: WebStack, name: str = "pizza-fan-site"):
        plan = build_stack_plan(stack, "acme/widgets")
        app_dir = scaffold_app(self.apps_dir, name, plan, _content())
        return app_dir, plan

    # --- CRA + MUI default ------------------------------------------------

    def test_cra_declares_build_and_pages_homepage(self):
        app_dir, plan = self._scaffold(WebStack.CRA_MUI)
        package = json.loads((app_dir / "package.json").read_text())
        self.assertEqual(package["homepage"], plan.public_path)
        self.assertEqual(package["homepage"], "/widgets/")
        self.assertEqual(package["scripts"]["build"], "react-scripts build")

    def test_cra_declares_react_scripts_as_a_dependency(self):
        """FR-3.4: `npm install && npm run build` must actually run —
        the build tool has to be a declared dependency, not just a
        script name (fake npm cannot catch a missing one)."""
        app_dir, _ = self._scaffold(WebStack.CRA_MUI)
        package = json.loads((app_dir / "package.json").read_text())
        declared = {**package.get("dependencies", {}),
                    **package.get("devDependencies", {})}
        self.assertIn("react-scripts", declared)

    def test_cra_source_carries_the_dark_theme_and_content(self):
        app_dir, _ = self._scaffold(WebStack.CRA_MUI)
        source = (app_dir / "src" / "App.js").read_text()
        self.assertIn(CRA_DARK_THEME_SNIPPET, source)
        self.assertIn("createTheme({ palette: { mode: 'dark' } })", source)
        self.assertIn("content.json", source)  # content is referenced

    def test_cra_content_module_is_the_slice_6_payload(self):
        app_dir, _ = self._scaffold(WebStack.CRA_MUI)
        written = json.loads((app_dir / "src" / "content.json").read_text())
        self.assertEqual(written, PAYLOAD)

    # --- Vue ---------------------------------------------------------------

    def test_vue_plan_files(self):
        app_dir, _ = self._scaffold(WebStack.VUE)
        config = (app_dir / "vite.config.js").read_text()
        self.assertIn("base: '/widgets/'", config)
        package = json.loads((app_dir / "package.json").read_text())
        self.assertEqual(package["scripts"]["build"], "vite build")
        self.assertIn("content.json", (app_dir / "src" / "App.vue")
                      .read_text())
        written = json.loads((app_dir / "src" / "content.json").read_text())
        self.assertEqual(written, PAYLOAD)

    # --- plain static HTML --------------------------------------------------

    def test_plain_html_needs_no_build_files(self):
        app_dir, plan = self._scaffold(WebStack.PLAIN_HTML)
        self.assertFalse(plan.needs_build)
        self.assertTrue((app_dir / "index.html").exists())
        written = json.loads((app_dir / "content.json").read_text())
        self.assertEqual(written, PAYLOAD)
        self.assertFalse((app_dir / "package.json").exists())

    # --- confinement and data-only content (FR-3.3, FR-4.3) -----------------

    def test_unsafe_app_names_are_rejected(self):
        for bad in ("../evil", "a/b", "", "Pizza", ".hidden", "x"[:0]):
            with self.assertRaises(ScaffoldPathError, msg=bad):
                validate_app_name(bad)

    def test_scaffold_with_unsafe_name_creates_nothing(self):
        with self.assertRaises(ScaffoldPathError):
            scaffold_app(self.apps_dir, "../evil",
                         build_stack_plan(WebStack.CRA_MUI, "acme/widgets"),
                         _content())
        self.assertFalse((self.apps_dir.parent / "evil").exists())

    def test_content_reaches_the_app_as_json_data_only(self):
        # A payload carrying shell metacharacters is written verbatim as
        # JSON and appears in no executable position of the scaffold.
        app_dir, _ = self._scaffold(WebStack.CRA_MUI)
        raw = (app_dir / "src" / "content.json").read_text()
        self.assertIn("rm -rf /", raw)          # inert inside the data file
        source = (app_dir / "src" / "App.js").read_text()
        self.assertNotIn("rm -rf", source)      # never interpolated into code
        self.assertNotIn("exec", source)


if __name__ == "__main__":
    unittest.main()
