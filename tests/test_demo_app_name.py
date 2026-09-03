"""Slice 5 — demo app-name derivation (spec §4, edge case 8).

`derive_app_name` turns an issue title into a short kebab-case directory
name; `resolve_app_name` appends `-<issue-number>` when the derived name
is already taken in the apps directory. No git, no network.

Run from the repo root:  python3 -m unittest tests.test_demo_app_name
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.workflow.demo_app_name import (
    FALLBACK_APP_NAME,
    MAX_NAME_LENGTH,
    derive_app_name,
    resolve_app_name,
    resolve_generation_app_name,
)


class DeriveAppNameTest(unittest.TestCase):
    def test_plain_title_becomes_kebab_case(self):
        self.assertEqual(derive_app_name("Pizza Fan Site"), "pizza-fan-site")

    def test_punctuation_and_case_collapse(self):
        self.assertEqual(derive_app_name("  Pizza!! Fan & Site...  "),
                         "pizza-fan-site")

    def test_underscores_and_slashes_are_separators(self):
        self.assertEqual(derive_app_name("My_App / v2"), "my-app-v2")

    def test_digits_are_kept(self):
        self.assertEqual(derive_app_name("Top 10 Synthwave Tracks 2025"),
                         "top-10-synthwave-tracks-2025")

    def test_empty_and_nonsense_titles_fall_back(self):
        for title in ("", "   ", "🎉🎉", "———", "!!!"):
            self.assertEqual(derive_app_name(title), FALLBACK_APP_NAME,
                             f"title {title!r} did not fall back")

    def test_long_title_is_truncated_without_trailing_dash(self):
        name = derive_app_name("word " * 40)
        self.assertLessEqual(len(name), MAX_NAME_LENGTH)
        self.assertFalse(name.startswith("-"))
        self.assertFalse(name.endswith("-"))

    def test_none_title_falls_back(self):
        self.assertEqual(derive_app_name(None), FALLBACK_APP_NAME)


class ResolveAppNameTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apps_dir = Path(self._tmp.name) / "demo-apps"
        self.apps_dir.mkdir()

    def test_free_name_is_used_verbatim(self):
        self.assertEqual(resolve_app_name("Pizza Fan Site", 7, self.apps_dir),
                         "pizza-fan-site")
        self.assertFalse((self.apps_dir / "pizza-fan-site").exists())

    def test_collision_appends_issue_number(self):
        (self.apps_dir / "pizza-fan-site").mkdir()
        self.assertEqual(resolve_app_name("Pizza Fan Site", 42, self.apps_dir),
                         "pizza-fan-site-42")

    def test_missing_apps_dir_is_no_collision(self):
        self.assertEqual(
            resolve_app_name("Pizza Fan Site", 7, self.apps_dir / "absent"),
            "pizza-fan-site")


class ResolveGenerationAppNameTest(unittest.TestCase):
    """The final-generation name (FR-2.4, edge case 8): reuse this
    issue's placeholder directory; suffix only when the bare name
    belongs to another app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apps_dir = Path(self._tmp.name) / "demo-apps"
        self.apps_dir.mkdir()
        self.owners = {}  # dir name -> owning issue number, or None

    def owner_of(self, app_dir: Path):
        return self.owners.get(Path(app_dir).name)

    def resolve(self, issue: int = 9):
        return resolve_generation_app_name("Pizza Fan Site", issue,
                                           self.apps_dir, self.owner_of)

    def test_free_name_is_used_verbatim(self):
        self.assertEqual(self.resolve(), "pizza-fan-site")

    def test_own_placeholder_directory_is_reused(self):
        (self.apps_dir / "pizza-fan-site").mkdir()
        self.owners["pizza-fan-site"] = 9
        self.assertEqual(self.resolve(), "pizza-fan-site")

    def test_collision_placeholder_directory_is_reused(self):
        (self.apps_dir / "pizza-fan-site-9").mkdir()
        self.owners["pizza-fan-site-9"] = 9
        self.assertEqual(self.resolve(), "pizza-fan-site-9")

    def test_bare_name_owned_by_another_issue_is_suffixed(self):
        (self.apps_dir / "pizza-fan-site").mkdir()
        self.owners["pizza-fan-site"] = 77
        self.assertEqual(self.resolve(), "pizza-fan-site-9")

    def test_unstamped_bare_name_is_suffixed_never_clobbered(self):
        (self.apps_dir / "pizza-fan-site").mkdir()
        self.owners["pizza-fan-site"] = None
        self.assertEqual(self.resolve(), "pizza-fan-site-9")


if __name__ == "__main__":
    unittest.main()
