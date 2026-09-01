"""Slice 6 (001-full-interactions-logged): full acceptance sweep.

One in-process run of the whole feature set, per spec AC 1-9. A scripted fake
`pi` on `PATH` (never the real binary — `setUp` asserts `shutil.which` resolves
inside the temp dir) answers a queue of canned responses, so a single flow
exercises spec author → assessor kickback → retry → nested-fence output →
crashed session, exactly as a real pass would order them.

Covered here as a set (individual slices pin the details; this file proves the
combination):

- AC 1: one `NNN-*.md` transcript per session attempt under `artifacts/sessions/`
  with the exact prompt pi received, the exact assistant output, and metadata
  matching the stats row;
- AC 2: the retried spec author gets its own file (`-iter-2`), nothing overwritten;
- AC 3: an output containing ``` fences still yields balanced, valid Markdown;
- AC 4: the killed session still yields prompt + partial output + `## Stderr`
  and `crashed: true`;
- AC 5: `journey.md` exists after the pass with one session-table row per
  session and every Transcript link resolving relative to `journey.md`;
- AC 6: the Mermaid block declares one node per session, every declared node
  with a transcript has exactly one `click`, and hostile names never appear
  raw in the diagram;
- AC 7: no `.pi-session-*` capture survives in the workdir when `task_id` is
  set; the `task_id=None` session is pooled (FR-7) and its capture is removed
  once the pooled transcript is durable (FR-6);
- AC 8: the exec summary lists `journey.md` and `sessions/` and no longer
  mentions `*.out`;
- AC 9: a resumed task (transcripts restored, stats store gone) numbers past
  the restored files without touching them.

Run from the repo root:  python3 -m unittest tests.test_transcript_acceptance
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core import prompts
from harness.core.enums import Stage
from harness.core.providers import Task
from harness.core.session import SessionRunner
from harness.core.stats import (
    StatsStore,
    render_task_journey_markdown,
)
from harness.workflow.pipeline import Pipeline
from harness.workflow.task_lifecycle import TaskLifecycle

from tests.test_journey_markdown import _markdown_links
from tests.test_transcript_basic import _cfg

PROMPT_SEPARATOR = "\x00"

SPEC_OUT = "Spec written.\n\nVERDICT: done"
KICKBACK_OUT = "Missing the timeout edge case.\n\nVERDICT: kickback"
REVISED_OUT = "Revised with the timeout case.\n\nVERDICT: done"
FENCED_OUT = (
    "Here is a sample implementation:\n\n"
    "```python\n"
    "def f():\n"
    "    return 1\n"
    "```\n"
    "Done.\n\nVERDICT: done"
)
PARTIAL_OUT = "Partial answer before death."
CRASH_STDERR = "pi: fatal: killed by upstream OOM\n"


def _scripted_pi(bin_dir: Path, script_path: Path, prompt_log: Path) -> None:
    """An executable `pi` that replays one JSON response per invocation.

    `script_path` holds a JSON list of response objects; each invocation pops
    the first, logs the `-p` prompt it received (so the test compares the
    transcript against what actually crossed the argv boundary), writes stderr,
    optionally prints a partial `message_end` event and SIGKILLs itself, then
    prints its `message_end` and exits with `rc`.
    """
    body = textwrap.dedent(f"""
        import json, os, signal, sys
        if "--list-models" in sys.argv:
            print("m")
            sys.exit(0)
        with open({str(script_path)!r}) as fh:
            script = json.load(fh)
        response = script.pop(0)
        with open({str(script_path)!r}, "w") as fh:
            json.dump(script, fh)
        prompt = sys.argv[sys.argv.index("-p") + 1]
        with open({str(prompt_log)!r}, "a") as fh:
            fh.write(prompt + {PROMPT_SEPARATOR!r})
        if response.get("stderr"):
            sys.stderr.write(response["stderr"])
        if response.get("partial"):
            print(json.dumps({{"type": "message_end", "message": {{
                "role": "assistant",
                "usage": {{"totalTokens": response.get("tokens", 7)}},
                "content": [{{"type": "text", "text": response["partial"]}}],
            }}}}), flush=True)
        if response.get("kill"):
            sys.stderr.flush()
            os.kill(os.getpid(), signal.SIGKILL)
        if response.get("output") is not None:
            print(json.dumps({{"type": "message_end", "message": {{
                "role": "assistant",
                "usage": {{"totalTokens": response.get("tokens", 7)}},
                "content": [{{"type": "text", "text": response["output"]}}],
            }}}}), flush=True)
        sys.exit(response.get("rc", 0))
    """)
    (bin_dir / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "try:\n"
        + textwrap.indent(body.strip("\n"), "    ") + "\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (bin_dir / "pi").chmod(0o755)


def _flow_script() -> list[dict]:
    """spec → assessor kickback → spec retry → fenced output → killed session."""
    return [
        {"output": SPEC_OUT, "tokens": 100},
        {"output": KICKBACK_OUT, "tokens": 80},
        {"output": REVISED_OUT, "tokens": 120},
        {"output": FENCED_OUT, "tokens": 90},
        {"partial": PARTIAL_OUT, "stderr": CRASH_STDERR, "kill": True,
         "tokens": 40},
    ]


def _fence_sections(text: str) -> list[tuple[str, str]]:
    """(delimiter, body) for every fenced block in a Markdown document."""
    sections = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = re.match(r"^(`{3,})\s*$", lines[i])
        if match:
            fence = match.group(1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != fence:
                body.append(lines[i])
                i += 1
            sections.append((fence, "\n".join(body)))
        i += 1
    return sections


class AcceptanceSweepTest(unittest.TestCase):
    """The five-session flow, then one assertion group per acceptance criterion."""

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
        self.sessions_dir = self.art_dir / "sessions"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        self.script_path = self.work_dir / "script.json"
        self.prompt_log = self.work_dir / "prompts.log"
        _scripted_pi(self.bin_dir, self.script_path, self.prompt_log)
        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or Path(found).resolve().parent != self.bin_dir.resolve():
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          "refusing to invoke a real model")

        self.cfg = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)

    # ------------------------------------------------------------------
    # the flow
    # ------------------------------------------------------------------
    def _run_flow(self) -> None:
        self.script_path.write_text(json.dumps(_flow_script()))
        self.runner.run("m", self.work_repo, "Author the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR)
        self.runner.run("m", self.work_repo, "Assess the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_ASSESS_TW)
        self.runner.run("m", self.work_repo, "Revise the spec.",
                        task_id=self.task_id, stage=Stage.SPEC_AUTHOR,
                        iteration=2)
        self.runner.run("m", self.work_repo, "Implement slice 1.",
                        task_id=self.task_id, stage=Stage.SLICE_IMPLEMENT,
                        slice_id="1")
        self.runner.run("m", self.work_repo, "Review slice 1.",
                        task_id=self.task_id, stage=Stage.TECH_REVIEW,
                        slice_id="1")
        self.pipeline._persist_journey_readout(self.task_id, self.task_dir)

    def _prompts_sent(self) -> list[str]:
        return self.prompt_log.read_text().split(PROMPT_SEPARATOR)[:-1]

    # ------------------------------------------------------------------
    # AC 1: one transcript per attempt, exact prompt/output, metadata = row
    # ------------------------------------------------------------------
    def test_ac1_one_transcript_per_attempt_with_exact_prompt_and_output(self):
        self._run_flow()
        names = sorted(p.name for p in self.sessions_dir.glob("*.md"))
        self.assertEqual(names, [
            "001-spec_author.md",
            "002-spec_assess_tw.md",
            "003-spec_author-iter-2.md",
            "004-slice_implement-slice-1.md",
            "005-tech_review-slice-1.md",
        ], f"transcript set wrong: {self.lines}")

        text = (self.sessions_dir / "001-spec_author.md").read_text()
        self.assertIn(f"# Session 001: spec_author ({self.task_id})", text)
        for line in ("- stage: spec_author", "- iteration: 1", "- model: m",
                     "- peak_tokens: 100", "- rc: 0", "- verdict: done",
                     "- crashed: false"):
            self.assertIn(line, text)

        # The transcript's Prompt is byte-identical to what the fake pi actually
        # received on argv — context-budget note included — and Output to the
        # exact assistant text.
        sections = _fence_sections(text)
        self.assertEqual(sections[0][1], self._prompts_sent()[0])
        self.assertEqual(sections[0][1],
                         prompts.CONTEXT_BUDGET_NOTE.format(
                             budget_k=self.cfg.model_budget("m") // 1000)
                         + "Author the spec.")
        self.assertEqual(sections[1][1], SPEC_OUT)

    def test_ac1_metadata_matches_the_stats_rows(self):
        self._run_flow()
        rows = self.store.for_task(self.task_id)
        self.assertEqual(len(rows), 5)
        by_stage = {r["stage"] + f"-{r['iteration']}": r for r in rows}
        text = (self.sessions_dir / "003-spec_author-iter-2.md").read_text()
        row = by_stage["spec_author-2"]
        self.assertIn(f"- peak_tokens: {row['peak_tokens']}", text)
        self.assertIn(f"- rc: {row['rc']}", text)
        self.assertIn(f"- verdict: {row['verdict']}", text)
        self.assertIn(f"- model: {row['model']}", text)

    # ------------------------------------------------------------------
    # AC 2: retried attempts never overwrite
    # ------------------------------------------------------------------
    def test_ac2_kickback_retry_gets_its_own_transcript(self):
        self._run_flow()
        first = (self.sessions_dir / "001-spec_author.md").read_text()
        retry = (self.sessions_dir / "003-spec_author-iter-2.md").read_text()
        self.assertIn("- iteration: 1", first)
        self.assertIn("- iteration: 2", retry)
        self.assertIn(SPEC_OUT, first)
        self.assertIn(REVISED_OUT, retry)
        self.assertNotEqual(first, retry)

    # ------------------------------------------------------------------
    # AC 3: nested fences stay valid Markdown
    # ------------------------------------------------------------------
    def test_ac3_nested_fences_render_with_a_longer_delimiter(self):
        self._run_flow()
        text = (self.sessions_dir / "004-slice_implement-slice-1.md").read_text()
        sections = _fence_sections(text)
        self.assertEqual(len(sections), 2, "Prompt + Output, no Stderr section")
        output_fence, output_body = sections[1]
        self.assertEqual(output_body, FENCED_OUT)
        self.assertGreater(len(output_fence), 3,
                           "output containing ``` must use a longer fence")
        for fence, _ in sections:
            self.assertGreaterEqual(len(fence), 3)

    # ------------------------------------------------------------------
    # AC 4: the killed session still yields a full transcript
    # ------------------------------------------------------------------
    def test_ac4_crashed_session_keeps_prompt_output_stderr(self):
        self._run_flow()
        text = (self.sessions_dir / "005-tech_review-slice-1.md").read_text()
        self.assertIn("- crashed: true", text)
        sections = _fence_sections(text)
        self.assertEqual(len(sections), 3, "Prompt + Output + Stderr")
        self.assertIn("Review slice 1.", sections[0][1])
        self.assertEqual(sections[1][1], PARTIAL_OUT)
        self.assertEqual(sections[2][1], CRASH_STDERR)

    # ------------------------------------------------------------------
    # AC 5: journey.md links resolve, one row per session
    # ------------------------------------------------------------------
    def test_ac5_journey_md_links_resolve_one_row_per_session(self):
        self._run_flow()
        journey_md = self.art_dir / "journey.md"
        self.assertTrue(journey_md.is_file(), f"journey.md missing: {self.lines}")
        text = journey_md.read_text()
        body_rows = [ln for ln in text.splitlines()
                     if ln.startswith("| ") and not ln.startswith("| # ")
                     and not ln.startswith("| ---")]
        self.assertEqual(len(body_rows), 5)
        links = _markdown_links(text)
        self.assertEqual(len(links), 5)
        for link in links:
            self.assertFalse(link.startswith("/"))
            self.assertTrue((journey_md.parent / link).is_file(),
                            f"link does not resolve: {link}")

    # ------------------------------------------------------------------
    # AC 6: the Mermaid block parses and escapes hostile names
    # ------------------------------------------------------------------
    def test_ac6_every_declared_node_has_exactly_one_click(self):
        self._run_flow()
        text = (self.art_dir / "journey.md").read_text()
        block = re.search(r"```mermaid\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(block, "no mermaid block in journey.md")
        body = block.group(1)
        self.assertTrue(body.startswith("flowchart"))
        nodes = re.findall(r'^\s+(N\d+)\["', body, re.MULTILINE)
        self.assertEqual(len(nodes), 5)
        for node in nodes:
            clicks = re.findall(rf"^\s+click {node} \"", body, re.MULTILINE)
            self.assertEqual(len(clicks), 1,
                             f"node {node} needs exactly one click line")

    def test_ac6_hostile_names_never_appear_raw_in_the_diagram(self):
        rows = [
            {"ts": "2026-08-26T10:00:00+0000", "task_id": "t1",
             "stage": 'we"ird[stage]#x', "model": "m", "verdict": 'p"a[s]s',
             "outcome": 'p"a[s]s', "peak_tokens": 10, "duration_s": 1.0,
             "rc": 0, "iteration": 1, "notes": ""},
            {"ts": "2026-08-26T10:01:00+0000", "task_id": "t1",
             "stage": "tech_review", "model": "m", "verdict": "fail",
             "outcome": 'kick"back|splode', "peak_tokens": 10,
             "duration_s": 1.0, "rc": 0, "iteration": 1, "notes": ""},
        ]
        text = render_task_journey_markdown(rows, task_id="t1",
                                            transcript_files=[None, None])
        body = re.search(r"```mermaid\n(.*?)\n```", text, re.DOTALL).group(1)
        for hostile in ('we"ird', "[stage]", "p\"a[s]s", "kick\"back|splode"):
            self.assertNotIn(hostile, body)
        self.assertIn("#quot;", body)
        self.assertIn("#91;", body)

    # ------------------------------------------------------------------
    # AC 7: workdir capture
    # ------------------------------------------------------------------
    def test_ac7_no_hidden_capture_survives_with_task_id(self):
        self._run_flow()
        leftovers = list(self.work_repo.glob(".pi-session-*"))
        self.assertEqual(leftovers, [],
                         f"hidden capture files survived: {leftovers}")

    def test_ac7_task_id_none_pools_transcript_and_removes_capture(self):
        self.script_path.write_text(json.dumps(
            [{"output": SPEC_OUT, "tokens": 10}]))
        result = self.runner.run("m", self.work_repo, "Direct use.",
                                 stage="manual")
        self.assertTrue(result.ok)
        self.assertEqual(list(self.sessions_dir.glob("*.md")), [],
                         "no task transcript without a task id")
        pool = list((self.work_dir / "artifacts" / "sessions")
                    .glob("*-manual.md"))
        self.assertEqual(len(pool), 1,
                         f"pooled transcript missing: {self.lines}")
        # FR-6: the pooled transcript is the durable copy; the capture goes.
        self.assertEqual(list(self.work_repo.glob(".pi-session-*")), [])

    # ------------------------------------------------------------------
    # AC 8: truthful exec summary
    # ------------------------------------------------------------------
    def test_ac8_exec_summary_lists_journey_and_sessions(self):
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.work_dir / "queue" / sub).mkdir(parents=True, exist_ok=True)
        lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        lifecycle.intake(Task(id="t8", body="# t8\n\nbody",
                              source="directory:t8.md"))
        lifecycle.park("t8", "acceptance sweep park")
        text = lifecycle.review_summary_path("t8").read_text()
        td = self.work_dir / "queue" / "parked" / "t8"
        self.assertIn(f"- journey: `{td}/artifacts/journey.md`", text)
        self.assertIn(f"- session transcripts: `{td}/artifacts/sessions/`", text)
        self.assertNotIn("*.out", text)

    # ------------------------------------------------------------------
    # AC 9: resume preserves transcripts, numbering continues
    # ------------------------------------------------------------------
    def test_ac9_resume_continues_numbering_past_restored_files(self):
        self._run_flow()
        restored = sorted(p.name for p in self.sessions_dir.glob("*.md"))
        self.assertEqual(len(restored), 5)
        before = {p.name: p.read_text() for p in self.sessions_dir.glob("*.md")}

        # Simulate the resume posture: transcripts are back on disk (the resume
        # path backs up and restores `artifacts/`), the process starts fresh.
        # `next_sequence` must use the on-disk count, not the stats rows.
        fresh_store = StatsStore(self.work_dir / "fresh-stats" / "sessions.jsonl")
        runner = SessionRunner(self.cfg, fresh_store, log=self.lines.append)
        self.script_path.write_text(json.dumps(
            [{"output": "Post-resume work.\n\nVERDICT: done", "tokens": 55}]))
        runner.run("m", self.work_repo, "Continue after resume.",
                   task_id=self.task_id, stage=Stage.SLICE_IMPLEMENT,
                   slice_id="1")

        after = {p.name: p.read_text() for p in self.sessions_dir.glob("*.md")}
        for name, content in before.items():
            self.assertEqual(after.get(name), content,
                             f"restored transcript changed: {name}")
        new_files = set(after) - set(before)
        self.assertEqual(new_files, {"006-slice_implement-slice-1.md"},
                         f"resume numbering wrong: {new_files}")


if __name__ == "__main__":
    unittest.main()
