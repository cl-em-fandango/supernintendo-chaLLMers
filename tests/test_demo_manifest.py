"""Slice 8 — the active-app manifest `demo-apps/DEPLOYED.json` (FR-7.1).

The manifest is the single answer to "which app is the active one": the
generation hook writes it beside the app source, the final-deploy hook
reads it, and the deployer builds exactly the named app. These tests
cover the shape on disk, the round trip, atomicity, and the rejection of
unsafe or corrupt manifests. No git, no npm, no pi.

Run from the repo root:  python3 -m unittest tests.test_demo_manifest
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.workflow.demo_manifest import (
    MANIFEST_NAME,
    ActiveAppManifest,
    ManifestError,
    manifest_path,
    read_manifest,
    write_manifest,
)


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.apps_dir = Path(self._tmp.name) / "demo-apps"

    def test_round_trip(self):
        manifest = ActiveAppManifest(app="pizza-fan-site", issue=7,
                                     task="pizza_fan_site")
        write_manifest(self.apps_dir, manifest)
        self.assertEqual(read_manifest(self.apps_dir), manifest)

    def test_file_shape_is_the_fr_7_1_document(self):
        write_manifest(self.apps_dir,
                       ActiveAppManifest(app="pizza-fan-site", issue=7,
                                         task="pizza_fan_site"))
        raw = json.loads(
            (self.apps_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(raw, {"app": "pizza-fan-site", "issue": 7,
                               "task": "pizza_fan_site"})

    def test_missing_manifest_reads_as_none(self):
        self.assertIsNone(read_manifest(self.apps_dir))
        self.assertIsNone(read_manifest(Path(self._tmp.name) / "absent"))

    def test_write_replaces_the_previous_active_app(self):
        write_manifest(self.apps_dir,
                       ActiveAppManifest(app="old-app", issue=1, task="t1"))
        write_manifest(self.apps_dir,
                       ActiveAppManifest(app="new-app", issue=2, task="t2"))
        self.assertEqual(read_manifest(self.apps_dir).app, "new-app")

    def test_atomic_write_leaves_no_temp_file(self):
        write_manifest(self.apps_dir,
                       ActiveAppManifest(app="pizza", issue=1, task="t"))
        leftovers = [p.name for p in self.apps_dir.iterdir()]
        self.assertEqual(leftovers, [MANIFEST_NAME])

    def test_write_rejects_unsafe_app_names(self):
        for bad in ("../evil", "a/b", "", "Pizza", ".hidden"):
            with self.assertRaises(ManifestError, msg=bad):
                write_manifest(self.apps_dir,
                               ActiveAppManifest(app=bad, issue=1, task="t"))

    def test_write_rejects_empty_task_id(self):
        with self.assertRaises(ManifestError):
            write_manifest(self.apps_dir,
                           ActiveAppManifest(app="pizza", issue=1, task=""))

    def test_corrupt_manifest_raises_not_silently_none(self):
        # Which app to build must never be guessed: an unreadable
        # manifest blocks the deploy instead of reading as "no manifest".
        manifest_path(self.apps_dir).parent.mkdir(parents=True)
        manifest_path(self.apps_dir).write_text("{not json",
                                                encoding="utf-8")
        with self.assertRaises(ManifestError):
            read_manifest(self.apps_dir)

    def test_unsafe_app_name_in_a_manifest_file_is_rejected(self):
        manifest_path(self.apps_dir).parent.mkdir(parents=True)
        manifest_path(self.apps_dir).write_text(
            json.dumps({"app": "../evil", "issue": 1, "task": "t"}),
            encoding="utf-8")
        with self.assertRaises(ManifestError):
            read_manifest(self.apps_dir)

    def test_non_integer_issue_is_rejected(self):
        manifest_path(self.apps_dir).parent.mkdir(parents=True)
        manifest_path(self.apps_dir).write_text(
            json.dumps({"app": "pizza", "issue": "seven", "task": "t"}),
            encoding="utf-8")
        with self.assertRaises(ManifestError):
            read_manifest(self.apps_dir)


if __name__ == "__main__":
    unittest.main()
