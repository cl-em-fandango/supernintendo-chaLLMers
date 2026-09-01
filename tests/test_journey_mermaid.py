"""Slice 5 (001-full-interactions-logged): the Mermaid flowchart in journey.md.

Pins FR-3.2 / AC 6:

- `journey.md` contains a ```mermaid block starting with `flowchart`;
- one node per session in chronological order, labels carrying stage, slice,
  iteration and verdict;
- every node declared in the flowchart has exactly one `click` line pointing
  at its transcript (`click N4 "sessions/004-….md"`), except nodes whose
  session has no transcript — those get none;
- names containing `"`, `[`, `]` or `#` never appear raw inside the diagram:
  they are Mermaid entities, so the diagram parses regardless of input;
- the block survives the real pipeline hook (fake `pi` on `PATH`, never the
  real binary) and its click targets resolve inside `artifacts/`.

Run from the repo root:  python3 -m unittest tests.test_journey_mermaid
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.config import Config
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import (
    StatsStore,
    render_task_journey_markdown,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_journey_markdown import (  # noqa: E402
    _cfg,
    _journey_rows,
    _write_fake_pi,
)

MERMAID_BLOCK_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
NODE_RE = re.compile(r"^\s*(N\d+)\[\"([^\"\n]*)\"\]\s*$", re.MULTILINE)
CLICK_RE = re.compile(r"^\s*click (N\d+) \"([^\"\n]+)\"\s*$", re.MULTILINE)


def _mermaid_body(text: str) -> str:
    """The diagram source inside the ```mermaid fence."""
    match = MERMAID_BLOCK_RE.search(text)
    assert match, f"no mermaid block in:\n{text}"
    return match.group(1)


class MermaidBlockTest(unittest.TestCase):
    def setUp(self):
        self.files = ["001-spec_author.md", "002-spec_assess_tw.md", None,
                      "004-slice_implement-slice-1.md"]
        self.text = render_task_journey_markdown(
            _journey_rows(), task_id="001-test", transcript_files=self.files)
        self.body = _mermaid_body(self.text)

    def test_block_starts_with_a_flowchart_declaration(self):
        self.assertTrue(self.body.startswith("flowchart "),
                        f"block starts with: {self.body.splitlines()[0]!r}")

    def test_one_node_per_session_in_chronological_order(self):
        nodes = NODE_RE.findall(self.body)
        self.assertEqual([node_id for node_id, _ in nodes],
                         ["N1", "N2", "N3", "N4"])

    def test_labels_carry_target_and_verdict(self):
        labels = dict(NODE_RE.findall(self.body))
        self.assertEqual(labels["N1"], "spec_author → done")
        self.assertIn("slice 1: implement → done", labels["N4"])
        self.assertIn("spec_author (iter 2)", labels["N3"])

    def test_every_node_with_a_transcript_has_exactly_one_click(self):
        clicks = CLICK_RE.findall(self.body)
        self.assertEqual([node_id for node_id, _ in clicks],
                         ["N1", "N2", "N4"])
        self.assertEqual(dict(clicks)["N4"],
                         "sessions/004-slice_implement-slice-1.md")
        declared = {node_id for node_id, _ in NODE_RE.findall(self.body)}
        clicked = {node_id for node_id, _ in clicks}
        self.assertTrue(clicked <= declared)
        self.assertEqual(clicked, {"N1", "N2", "N4"},
                         "N3 has no transcript, so it must not be clicked")

    def test_no_click_line_without_a_declared_node(self):
        declared = {node_id for node_id, _ in NODE_RE.findall(self.body)}
        for node_id, _ in CLICK_RE.findall(self.body):
            self.assertIn(node_id, declared)

    def test_bounce_and_retry_edges_are_visible(self):
        self.assertIn("N2 -.->|kickback| N3", self.body)
        self.assertIn("N1 --> N2", self.body)
        self.assertIn("N3 --> N4", self.body)

    def test_single_session_yields_a_node_and_no_edges(self):
        text = render_task_journey_markdown([_journey_rows()[0]],
                                            task_id="001-test")
        body = _mermaid_body(text)
        self.assertEqual(len(NODE_RE.findall(body)), 1)
        self.assertNotIn("-->", body.replace("-.->", ""))

    def test_no_transcripts_yields_no_click_lines(self):
        text = render_task_journey_markdown(_journey_rows(), task_id="001-test")
        body = _mermaid_body(text)
        self.assertEqual(CLICK_RE.findall(body), [])
        self.assertEqual(len(NODE_RE.findall(body)), 4)


class MermaidSanitizationTest(unittest.TestCase):
    def _hostile_body(self) -> str:
        rows = _journey_rows()
        rows[0]["stage"] = 'we"ird[stage]#x'
        rows[0]["verdict"] = 'p"a[s]s`worse#'
        rows[1]["outcome"] = 'kick"back|splode'
        return _mermaid_body(render_task_journey_markdown(rows,
                                                          task_id="001-test"))

    def test_raw_injection_characters_never_appear_in_labels(self):
        body = self._hostile_body()
        for raw in ('we"ird[stage]#x', 'p"a[s]s`worse#', 'kick"back|splode'):
            self.assertNotIn(raw, body,
                             f"raw characters leaked into the diagram: {raw}")

    def test_labels_stay_well_formed_after_escaping(self):
        body = self._hostile_body()
        nodes = NODE_RE.findall(body)
        self.assertEqual(len(nodes), 4, "a hostile label broke a node line")
        for _node_id, label in nodes:
            for char in ('"', "[", "]", "`"):
                self.assertNotIn(char, label)
        self.assertIn("#quot;", body)
        self.assertIn("#91;", body)
        self.assertIn("#93;", body)
        self.assertIn("#35;", body)

    def test_hostile_edge_label_cannot_open_a_new_diagram_line(self):
        body = self._hostile_body()
        edge = [ln for ln in body.splitlines() if "N2 -.->" in ln][0]
        self.assertEqual(edge.strip(), "N2 -.->|kick#quot;back#124;splode| N3")

    def test_newlines_in_a_name_cannot_split_a_line(self):
        rows = _journey_rows()
        rows[0]["stage"] = "line\nbreak"
        body = _mermaid_body(render_task_journey_markdown(rows,
                                                          task_id="001-test"))
        self.assertIn('N1["line break → done"]', body)


class MermaidPipelineTest(unittest.TestCase):
    """The block lands in `journey.md` through the real finally-hook."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.task_id = "t1"
        self.task_dir = self.work_dir / "queue" / "active" / self.task_id
        (self.task_dir / "artifacts" / "sessions").mkdir(parents=True)
        self.art_dir = self.task_dir / "artifacts"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        _write_fake_pi(self.bin_dir)
        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or Path(found).resolve().parent != self.bin_dir.resolve():
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          "refusing to invoke a real model")

        from harness.workflow.pipeline import Pipeline
        self.cfg = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)

    def test_journey_md_mermaid_clicks_resolve_to_real_transcripts(self):
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR)
        self.runner.run("m", self.work_repo, "Implement slice 1.",
                        task_id=self.task_id, stage=Stage.SLICE_IMPLEMENT,
                        slice_id="1")
        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

        body = _mermaid_body((self.art_dir / "journey.md").read_text())
        clicks = CLICK_RE.findall(body)
        self.assertEqual([node_id for node_id, _ in clicks], ["N1", "N2"])
        for _node_id, target in clicks:
            self.assertFalse(target.startswith("/"),
                             f"click target must be relative: {target}")
            self.assertTrue((self.art_dir / target).is_file(),
                            f"click target does not resolve: {target}")


if __name__ == "__main__":
    unittest.main()
