"""Slice 1 (001-full-interactions-logged): FR-5 regression — the artifacts
directory must not end up empty (spec AC 1-3, 9).

The reported defect was a real ~25-session run whose `artifacts/sessions/`
held only the two newest transcripts and whose `journey.md` contained zero
`sessions/…` links. Unit tests of the renderer could not see that failure:
only a full pass through `Pipeline.process` with a real `SessionRunner` can.

This file drives exactly that: a scripted fake `pi` on `PATH` (never the real
binary — `setUp` asserts `shutil.which` resolves inside the temp dir), a temp
queue/stats tree, and one `process()` call that walks the spec, feasibility,
slicing and one full slice loop, then parks at the holistic review. It asserts

- `artifacts/sessions/` holds exactly one `NNN-*.md` per session, numbered
  `001…` in chronological order, in the task's *current* (parked) directory;
- `journey.md` lands beside them, carries a `flowchart LR` Mermaid block, and
  every Markdown link and Mermaid `click` target resolves to a file inside the
  same `artifacts/` directory;
- the transcript's `## Prompt` section holds the exact prompt the fake `pi`
  received across argv (context-budget preamble included).

Run from the repo root:  python3 -m unittest tests.test_transcripts_end_to_end
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core.providers import Task
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.workflow.pipeline import Pipeline

from tests.test_journey_markdown import _markdown_links
from tests.test_transcript_basic import _cfg

PROMPT_SEPARATOR = "\x00"

# One scripted response per session, in the order the waterfall produces them:
# spec author -> ornith -> tw -> feasibility -> slicing -> slice check ->
# implement -> tech review -> func review -> holistic (fail, so the pass ends
# in `parked` and the journey must still land for a parked pass).
SLICES_BODY = "# Slices\n\n### Slice 1\n\nDo the thing.\n"


def _flow_script() -> list[dict]:
    return [
        {"output": "Spec written.\n\nVERDICT: done"},
        {"output": "Spec is complete.\n\nVERDICT: pass"},
        {"output": "Requirements covered.\n\nVERDICT: pass"},
        {"output": "Feasible.\n\nVERDICT: pass"},
        {"output": "Slices written.\n\nVERDICT: done", "write_slices": True},
        {"output": "Slices fit.\n\nVERDICT: pass"},
        {"output": "Slice implemented.\n\nVERDICT: done"},
        {"output": "Technical review clean.\n\nVERDICT: pass"},
        {"output": "Functional review clean.\n\nVERDICT: pass"},
        {"output": "Holistic check found a gap.\n\nVERDICT: fail"},
    ]

# The transcript filenames the waterfall above must produce, in order.
EXPECTED_TRANSCRIPTS = [
    "001-spec_author.md",
    "002-spec_assess_ornith.md",
    "003-spec_assess_tw.md",
    "004-feasibility.md",
    "005-slicing.md",
    "006-slice_check.md",
    "007-slice_implement-slice-1.md",
    "008-tech_review-slice-1.md",
    "009-func_review-slice-1.md",
    "010-holistic.md",
]


def _scripted_pi(bin_dir: Path, script_path: Path, prompt_log: Path,
                 slices_path: Path) -> None:
    """An executable `pi` replaying one JSON response per invocation.

    Each invocation pops the first response from `script_path`, logs the `-p`
    prompt it received (so the test can compare the transcript against what
    actually crossed argv), optionally writes `slices.md` (the slicing stage
    is only complete once the model has filed the file the pipeline parses),
    then prints a `message_end` event and exits.
    """
    body = textwrap.dedent(f"""
        import json, sys
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
        if response.get("write_slices"):
            with open({str(slices_path)!r}, "w") as fh:
                fh.write({SLICES_BODY!r})
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


def _make_repo(root: Path) -> Path:
    """A git repo with one commit on `pi/trunk` (what `ensure_branch` needs)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("target repo\n")
    subprocess.run(["git", "init", "-b", "pi/trunk"], cwd=root, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-m", "init"], cwd=root, check=True,
                   capture_output=True, text=True)
    return root


class EndToEndArtifactsTest(unittest.TestCase):
    """One full `process()` pass; the assertions are the FR-5 acceptance."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)

        self.queue_dir = self.work_dir / "queue"
        for sub in ("pending", "active", "done", "failed", "parked", "review"):
            (self.queue_dir / sub).mkdir(parents=True)

        self.task_id = "t1"
        self.active_dir = self.queue_dir / "active" / self.task_id
        self.parked_dir = self.queue_dir / "parked" / self.task_id
        self.art_dir = self.parked_dir / "artifacts"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        self.script_path = self.work_dir / "script.json"
        self.prompt_log = self.work_dir / "prompts.log"
        self.slices_path = self.active_dir / "artifacts" / "slices.md"
        _scripted_pi(self.bin_dir, self.script_path, self.prompt_log,
                     self.slices_path)
        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or Path(found).resolve().parent != self.bin_dir.resolve():
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          "refusing to invoke a real model")

        self.cfg = _cfg(self.work_dir)
        self.repo = _make_repo(self.work_dir / "repo")
        # Deterministic repo resolution (F7): the pipeline parks a task whose
        # workdir resolves under the queue, so the fixture must name the
        # target repo exactly like config.json `repoDir` would.
        self.cfg.repo_dir = self.repo
        self.store = StatsStore(self.cfg.stats_path)
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)
        self.pipeline = Pipeline(self.cfg, self.runner, log=self.lines.append)

    # ------------------------------------------------------------------
    # the pass
    # ------------------------------------------------------------------
    def _task(self) -> Task:
        return Task(id=self.task_id, body=f"# t1\n\nwork in {self.repo}\n",
                    source="directory:t1.md")

    def _run_pass(self) -> str:
        self.script_path.write_text(json.dumps(_flow_script()))
        status = self.pipeline.process(self._task())
        with open(self.script_path) as fh:
            remaining = json.load(fh)
        self.assertEqual(
            remaining, [],
            "the fake pi was not consulted exactly once per scripted session; "
            f"log:\n{''.join(self.lines)}")
        return status

    # ------------------------------------------------------------------
    # (a) transcripts land, numbered from 001, on every terminal path
    # ------------------------------------------------------------------
    def test_every_session_lands_a_transcript_numbered_from_001(self):
        status = self._run_pass()
        self.assertEqual(status, "parked",
                         "holistic was scripted to fail; the pass must park")
        sessions_dir = self.art_dir / "sessions"
        self.assertTrue(sessions_dir.is_dir(),
                        "a parked pass left no artifacts/sessions/ directory")
        landed = sorted(p.name for p in sessions_dir.glob("*.md"))
        self.assertEqual(landed, EXPECTED_TRANSCRIPTS,
                         "artifacts/sessions/ is not the full audit trail")
        # The task moved active/ -> parked/ mid-pass; the transcripts moved
        # with it and nothing was orphaned back in the old location.
        self.assertFalse((self.queue_dir / "active" / self.task_id).exists(),
                         "task dir survived after the park")
        self.assertEqual(len(self.store.for_task(self.task_id)),
                         len(EXPECTED_TRANSCRIPTS),
                         "one stats row per session expected")

    def test_transcript_holds_the_exact_prompt_across_argv(self):
        self._run_pass()
        prompts = self.prompt_log.read_text().split(PROMPT_SEPARATOR)
        first = prompts[0]
        transcript = (self.art_dir / "sessions" / EXPECTED_TRANSCRIPTS[0]).read_text()
        self.assertIn("## Prompt", transcript)
        self.assertIn(first, transcript,
                      "the transcript lost part of what pi actually received")
        self.assertIn("VERDICT: done", transcript,
                      "the transcript lost the assistant output")

    # ------------------------------------------------------------------
    # (b) journey.md links every transcript, and no link is broken
    # ------------------------------------------------------------------
    def test_journey_links_every_transcript_and_no_link_is_broken(self):
        self._run_pass()
        journey = self.art_dir / "journey.md"
        self.assertTrue(journey.is_file(),
                        "a parked pass left no journey.md beside its transcripts")
        text = journey.read_text()

        self.assertIn("flowchart LR", text,
                      "journey.md must carry the Mermaid flowchart")

        links = _markdown_links(text)
        self.assertEqual(sorted(links),
                         [f"sessions/{name}" for name in EXPECTED_TRANSCRIPTS],
                         "every session row must link its transcript")
        for link in links:
            self.assertFalse(link.startswith("/"),
                             f"link must be relative to artifacts/: {link}")
            self.assertTrue((self.art_dir / link).is_file(),
                            f"link does not resolve: {link}")

        clicks = re.findall(r'click\s+N\d+\s+"([^"]+)"', text)
        self.assertEqual(sorted(clicks),
                         [f"sessions/{name}" for name in EXPECTED_TRANSCRIPTS],
                         "every flowchart node must click to its transcript")
        for target in clicks:
            self.assertTrue((self.art_dir / target).is_file(),
                            f"mermaid click target does not resolve: {target}")


if __name__ == "__main__":
    unittest.main()
