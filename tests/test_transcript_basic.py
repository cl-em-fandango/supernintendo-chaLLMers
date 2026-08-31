"""Slice 1 (001-full-interactions-logged): transcripts land in artifacts/sessions/.

`SessionRunner.run` used to persist only the assistant output, as a hidden
`.pi-session-*.out` dotfile in the implementation workdir, and the prompt sent
to the model was never written anywhere at all — the stats row kept its length
only. Nothing about a session ever reached the task's `artifacts/` directory.

These tests pin the first slice of the fix, end-to-end through a real
`run_pi_session` subprocess driven by a fake `pi` script on `PATH` (never the
real binary — `setUp` asserts `shutil.which` resolves inside the temp dir):

- one `001-<stage>.md` transcript per session under
  `<queue>/active/<task>/artifacts/sessions/`, with the H1 title, the metadata
  list, the exact full prompt (context-budget note included) and the exact
  assistant output;
- two sequential sessions number `001`, `002`;
- `task_id=None` writes no transcript and keeps the legacy workdir `.out`
  placement untouched;
- task intake creates `artifacts/sessions/` so the first session lands there.

Run from the repo root:  python3 -m unittest tests.test_transcript_basic
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.core import prompts
from harness.core.config import Config
from harness.core.enums import Stage
from harness.core.providers import Task
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.workflow.task_lifecycle import TaskLifecycle

ASSISTANT_TEXT = "## Summary\nAll good.\n\nVERDICT: done"
PEAK_TOKENS = 42


def _cfg(work_dir: Path) -> Config:
    return Config(
        work_dir=work_dir,
        token_budget=60_000,
        max_spec_kickbacks=3,
        max_slice_implement=5,
        max_slice_tech_review=5,
        max_slice_func_review=5,
        max_slice_check_loops=3,
        autonomous_queue_target=5,
        trunk_branch="pi/trunk",
        task_provider="directory",
        directory_provider={},
        models={"technicalWriter": "m", "implementer": "m", "assessor": "m"},
        model_context_map={"m": 131_072},
    )


def _fake_pi(bin_dir: Path, prompt_capture: Path) -> None:
    """Write an executable `pi` that captures its `-p` prompt and replies.

    The reply is one `message_end` wire event carrying `ASSISTANT_TEXT` and a
    usage block, so `run_pi_session` parses it exactly like the real binary's
    JSON stream. The prompt is written verbatim to `prompt_capture` so the
    test can compare what pi received against what the transcript stores.
    """
    body = textwrap.dedent(f"""
        import json, sys
        prompt = sys.argv[sys.argv.index("-p") + 1]
        with open({str(prompt_capture)!r}, "w") as fh:
            fh.write(prompt)
        event = {{
            "type": "message_end",
            "message": {{
                "role": "assistant",
                "usage": {{"totalTokens": {PEAK_TOKENS}}},
                "content": [{{"type": "text", "text": {ASSISTANT_TEXT!r}}}],
            }},
        }}
        print(json.dumps(event))
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


class TranscriptBasicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.work_repo = self.work_dir / "repo"
        self.work_repo.mkdir()
        self.task_id = "t1"
        self.task_dir = self.work_dir / "queue" / "active" / self.task_id
        self.task_dir.mkdir(parents=True)
        self.sessions_dir = self.task_dir / "artifacts" / "sessions"

        self.bin_dir = self.work_dir / "bin"
        self.bin_dir.mkdir()
        self.prompt_capture = self.work_dir / "prompt.txt"
        _fake_pi(self.bin_dir, self.prompt_capture)

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

    def _run(self, prompt: str = "Implement the thing.", **kwargs):
        kwargs.setdefault("task_id", self.task_id)
        kwargs.setdefault("stage", Stage.SPEC_AUTHOR)
        return self.runner.run("m", self.work_repo, prompt, **kwargs)

    def test_session_writes_transcript_with_prompt_and_output(self):
        prompt_body = "Implement the thing."
        result = self._run(prompt_body)
        self.assertTrue(result.ok, f"session should succeed: {self.lines}")

        transcript = self.sessions_dir / "001-spec_author.md"
        self.assertTrue(transcript.is_file(),
                        f"transcript missing; sessions dir: {list(self.sessions_dir.iterdir()) if self.sessions_dir.is_dir() else None}")
        text = transcript.read_text()

        self.assertIn(f"# Session 001: spec_author ({self.task_id})", text)
        for key in ("timestamp", "stage", "iteration", "model", "duration_s",
                    "peak_tokens", "rc", "verdict", "crashed"):
            self.assertRegex(text, rf"(?m)^- {key}: \S")
        self.assertIn("- verdict: done", text)
        self.assertIn("- crashed: false", text)
        self.assertIn(f"- peak_tokens: {PEAK_TOKENS}", text)
        self.assertIn("- rc: 0", text)

        # The prompt pi actually received — budget note included — is stored
        # verbatim inside the Prompt fence.
        prompt_sent = self.prompt_capture.read_text()
        expected_note = prompts.CONTEXT_BUDGET_NOTE.format(
            budget_k=self.cfg.model_budget("m") // 1000)
        self.assertEqual(prompt_sent, expected_note + prompt_body)
        self.assertIn("## Prompt", text)
        self.assertIn(f"```\n{prompt_sent}\n```", text)

        self.assertIn("## Output", text)
        self.assertIn(f"```\n{ASSISTANT_TEXT}\n```", text)
        # Stderr was empty: no Stderr section.
        self.assertNotIn("## Stderr", text)

    def test_two_sequential_sessions_number_001_and_002(self):
        self._run("first")
        self._run("second", stage=Stage.SLICING)
        names = sorted(p.name for p in self.sessions_dir.iterdir())
        self.assertEqual(names, ["001-spec_author.md", "002-slicing.md"])

    def test_no_task_id_writes_no_transcript_and_keeps_legacy_out(self):
        result = self._run("direct use", task_id=None)
        self.assertTrue(result.ok)
        self.assertFalse(self.sessions_dir.exists())
        # Legacy behavior: the hidden .out capture stays in the workdir.
        legacy = list(self.work_repo.glob(".pi-session-*.out"))
        self.assertEqual(len(legacy), 1, f"legacy .out missing: {self.lines}")
        self.assertEqual(legacy[0].read_text(), ASSISTANT_TEXT)

    def test_intake_creates_sessions_dir(self):
        lifecycle = TaskLifecycle(self.cfg, log=self.lines.append)
        task_dir = lifecycle.intake(Task(id="t9", body="body"))
        self.assertTrue((task_dir / "artifacts" / "sessions").is_dir())
        self.assertTrue((task_dir / "artifacts" / "progress").is_dir())


if __name__ == "__main__":
    unittest.main()
