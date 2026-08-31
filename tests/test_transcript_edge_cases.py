"""Slice 2 (001-full-interactions-logged): transcripts are always valid, always written.

Slice 1 put the prompt and the output on disk. This slice pins the ways a real
session stops being a clean happy path:

- fence safety: each section's delimiter is computed from *its own* content, so
  an assistant reply containing ``` is wrapped in a longer fence and the
  Prompt / Output / Stderr delimiters never share a length;
- an empty, failed session (no output, no stderr, rc != 0) still yields every
  section header with an empty fence;
- a session that dies mid-stream still yields prompt + partial output +
  `## Stderr` and `crashed: true` (spec AC 4);
- a transcript we cannot write (read-only directory) logs a warning and leaves
  `SessionRunner.run` returning normally — the pipeline is never aborted by its
  own audit log;
- invalid UTF-8 on pi's stream becomes U+FFFD instead of raising
  UnicodeDecodeError out of the stream reader.

Driven by fake `pi` scripts on `PATH` (never the real binary — `setUp` asserts
`shutil.which` resolves inside the temp dir), in temp dirs only.

Run from the repo root:  python3 -m unittest tests.test_transcript_edge_cases
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

from harness.core.config import Config
from harness.core.enums import Stage
from harness.core.session import SessionRunner
from harness.core.stats import StatsStore
from harness.core.transcripts import TranscriptRecord, render_transcript

from tests.test_transcript_basic import _cfg


def _fake_pi(bin_dir: Path, body: str) -> None:
    """Write an executable `pi` running `body` with flushed streams.

    `body` is the whole script: it sees `sys`, and the harness contract it has
    to honour is only "print JSON wire events, exit when done".
    """
    (bin_dir / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys\n"
        "try:\n"
        + textwrap.indent(body.strip("\n"), "    ") + "\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (bin_dir / "pi").chmod(0o755)


def _message_event(text: str, tokens: int = 7) -> str:
    """Script line printing one `message_end` wire event carrying `text`."""
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"totalTokens": tokens},
            "content": [{"type": "text", "text": text}],
        },
    }
    return f"print({json.dumps(json.dumps(event))})"


OUTPUT_WITH_FENCES = """Reply that quotes Markdown itself:

```bash
echo nested
```

And a longer run: ```` four backticks ```` plus `one` inline.
"""
STDERR_WITH_LONG_FENCE = "warning: ``` and ```` and `````` inside stderr\n"
PARTIAL_OUTPUT = "Partial answer before death.\n"


def _section(text: str, header: str) -> tuple[str, str]:
    """Return (fence, body) of the fenced block under a `## ` header.

    Raises AssertionError when the header is missing, so a dropped section
    fails the test instead of silently returning empty strings.
    """
    if header not in text:
        raise AssertionError(f"section {header!r} missing from transcript")
    lines = text.split(header, 1)[1].splitlines()
    fence = next((ln for ln in lines if ln.strip() and set(ln.strip()) == {"`"}),
                 None)
    if fence is None:
        raise AssertionError(f"no fence under {header!r}")
    idx = lines.index(fence)
    body: list[str] = []
    for line in lines[idx + 1:]:
        if line.strip() == fence.strip():
            break
        body.append(line)
    return fence.strip(), "\n".join(body)


class TranscriptEdgeCaseTest(unittest.TestCase):
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
        # Placeholder so the PATH assertion below is meaningful; every test
        # overwrites it with the behaviour it is pinning.
        _fake_pi(self.bin_dir, "sys.exit(0)")

        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or Path(found).resolve().parent != self.bin_dir.resolve():
            self.skipTest(f"fake pi is not first on PATH (resolved {found!r}); "
                          "refusing to invoke a real model")

        self.cfg: Config = _cfg(self.work_dir)
        self.store = StatsStore(self.cfg.stats_path)
        self.lines: list[str] = []
        self.runner = SessionRunner(self.cfg, self.store, log=self.lines.append)

    def _run(self, prompt: str = "Do the thing.", **kwargs):
        kwargs.setdefault("task_id", self.task_id)
        kwargs.setdefault("stage", Stage.SPEC_AUTHOR)
        return self.runner.run("m", self.work_repo, prompt, **kwargs)

    def _only_transcript(self) -> str:
        files = sorted(self.sessions_dir.glob("*.md"))
        self.assertEqual(len(files), 1, f"expected one transcript: {self.lines}")
        return files[0].read_text(encoding="utf-8")

    # --- fence safety ----------------------------------------------------

    def test_each_section_gets_its_own_fence_length(self):
        _fake_pi(self.bin_dir, "\n".join([
            _message_event(OUTPUT_WITH_FENCES),
            f"sys.stderr.write({STDERR_WITH_LONG_FENCE!r})",
            "sys.exit(0)",
        ]))
        result = self._run("Prompt with `one` inline run.")
        self.assertTrue(result.ok, f"session should succeed: {self.lines}")
        text = self._only_transcript()

        prompt_fence, prompt_body = _section(text, "## Prompt")
        output_fence, output_body = _section(text, "## Output")
        stderr_fence, stderr_body = _section(text, "## Stderr")

        # Prompt's longest run is 1 -> the minimum fence of 3.
        self.assertEqual(prompt_fence, "```")
        # Output contains a 4-backtick run -> strictly longer delimiter.
        self.assertEqual(output_fence, "`" * 5)
        # Stderr contains a 6-backtick run -> its own, longer delimiter again.
        self.assertEqual(stderr_fence, "`" * 7)
        # Independence: no two sections share a delimiter here.
        self.assertEqual(len({prompt_fence, output_fence, stderr_fence}), 3)

        # Bodies survive verbatim — the fence escaped, the content did not.
        self.assertIn("```bash", output_body)
        self.assertIn("```` four backticks ````", output_body)
        self.assertEqual(output_body.strip(), OUTPUT_WITH_FENCES.strip())
        self.assertEqual(stderr_body.strip(), STDERR_WITH_LONG_FENCE.strip())
        self.assertTrue(prompt_body.strip().endswith("Prompt with `one` inline run."))

    def test_rendered_sections_are_balanced_for_nested_fences(self):
        """Render-level pin: a ``` inside every section still closes cleanly."""
        record = TranscriptRecord(
            sequence=1, task_id="t", stage="review", timestamp="now", model="m",
            duration_s=1.0, peak_tokens=0, rc=0, verdict="pass", crashed=False,
            prompt="```\nnot a fence\n```", output="````\n", stderr="",
        )
        text = render_transcript(record)
        self.assertEqual(_section(text, "## Prompt")[0], "`" * 4)
        self.assertEqual(_section(text, "## Output")[0], "`" * 5)
        self.assertNotIn("## Stderr", text)

    # --- empty and failed sessions ---------------------------------------

    def test_empty_output_empty_stderr_nonzero_rc_writes_every_section(self):
        _fake_pi(self.bin_dir, "sys.exit(3)")
        result = self._run()
        self.assertFalse(result.ok)
        text = self._only_transcript()

        for header in ("## Prompt", "## Output", "## Stderr"):
            fence, body = _section(text, header)
            self.assertTrue(fence, f"{header} should still carry a fence")
            if header != "## Prompt":
                self.assertEqual(body, "", f"{header} should be an empty fence")
        self.assertIn("- rc: 3", text)
        # The failure reason is metadata, never a fabricated Stderr body.
        self.assertIn("- error: pi exited rc=3", text)

    def test_successful_session_with_empty_stderr_omits_the_section(self):
        _fake_pi(self.bin_dir, _message_event("All quiet."))
        result = self._run()
        self.assertTrue(result.ok, f"session should succeed: {self.lines}")
        self.assertNotIn("## Stderr", self._only_transcript())

    def test_crashed_session_keeps_prompt_partial_output_and_stderr(self):
        _fake_pi(self.bin_dir, "\n".join([
            _message_event(PARTIAL_OUTPUT),
            # Flushed by hand: SIGKILL skips the wrapper's `finally`.
            "sys.stderr.write('dying now\\n')",
            "sys.stdout.flush()",
            "sys.stderr.flush()",
            "os.kill(os.getpid(), signal.SIGKILL)",
        ]))
        result = self._run("Finish this before you die.")
        self.assertFalse(result.ok)
        text = self._only_transcript()

        self.assertIn("# Session 001: spec_author (t1)", text)
        self.assertIn("- crashed: true", text)
        self.assertNotEqual(_section(text, "## Prompt")[1].strip(), "")
        self.assertIn(PARTIAL_OUTPUT.strip(), _section(text, "## Output")[1])
        self.assertIn("dying now", _section(text, "## Stderr")[1])
        self.assertRegex(text, r"(?m)^- error: \S")

    # --- failure posture --------------------------------------------------

    def test_unwritable_transcript_dir_warns_and_run_still_returns(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: directory permissions are not enforced")
        self.sessions_dir.mkdir(parents=True)
        self.sessions_dir.chmod(0o500)
        self.addCleanup(self.sessions_dir.chmod, 0o700)

        _fake_pi(self.bin_dir, _message_event("Output the model still got."))
        result = self._run()

        self.assertTrue(result.ok, f"session itself must still succeed: {self.lines}")
        self.assertEqual(result.output.strip(), "Output the model still got.")
        self.assertEqual(list(self.sessions_dir.glob("*.md")), [])
        warnings = [ln for ln in self.lines if "transcript write failed" in ln]
        self.assertEqual(len(warnings), 1, f"expected one warning: {self.lines}")
        self.assertIn("001-spec_author", warnings[0])
        # The stats row is still recorded — the session is not lost with its transcript.
        self.assertEqual(len(self.store.for_task(self.task_id)), 1)

    # --- encoding ----------------------------------------------------------

    def test_invalid_utf8_on_pi_stream_becomes_replacement_character(self):
        # One valid wire event whose text field carries two undecodable bytes.
        raw_event = (
            b'{"type":"message_end","message":{"role":"assistant",'
            b'"usage":{"totalTokens":9},"content":[{"type":"text",'
            b'"text":"bad\xff\xfe byte"}]}}'
        )
        _fake_pi(self.bin_dir,
                 f"sys.stdout.buffer.write({raw_event!r} + b'\\n')")
        result = self._run()
        self.assertTrue(result.ok, f"session should survive the bad byte: {self.lines}")
        self.assertIn("\ufffd", result.output)
        text = self._only_transcript()
        self.assertIn("\ufffd", text)
        self.assertIn("bad", text)
        self.assertIn("- peak_tokens: 9", text)


class TranscriptFenceHelperTest(unittest.TestCase):
    """The delimiter rule, without a subprocess."""

    def test_rendered_fence_is_strictly_longer_than_content(self):
        for content, expected in (
            ("", "```"),
            ("plain text", "```"),
            ("`one`", "```"),
            ("```", "````"),
            ("```` deep ````", "`" * 5),
        ):
            record = TranscriptRecord(
                sequence=2, task_id="t", stage="s", timestamp="now", model="m",
                duration_s=0.0, peak_tokens=0, rc=0, verdict="pass",
                crashed=False, prompt="p", output=content, stderr="",
            )
            with self.subTest(content=content):
                fence, body = _section(render_transcript(record), "## Output")
                self.assertEqual(fence, expected)
                self.assertEqual(body, content)


if __name__ == "__main__":
    unittest.main()
