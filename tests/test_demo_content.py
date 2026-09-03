"""Slice 6 — LLM content generation with the fallback topic (FR-4; AC 5).

Two layers, both in-process:

  * the real `run_pi_session` against a fake `pi` executable first on
    `PATH` (temp dir, never the real binary): valid JSON flows into the
    payload, fenced JSON parses, the `{"coherent": false}` sentinel /
    garbage twice / a non-zero exit produce the fallback document, and
    the model name reaches the child's argv from config;
  * the pure decision logic with an injected runner: an empty,
    whitespace-only or label-boilerplate-only body falls back *without*
    calling the model, one retry recovers a good answer, and the prompt
    carries the ticket text as a quoted JSON data block.

Shell safety (FR-4.3): the module source is scanned for `subprocess` /
`shell=True`, and a ticket body plus a model answer carrying shell
metacharacters are proven inert — the body reaches the child verbatim as
one argv element and no marker file is ever created.

Run from the repo root:  python3 -m unittest tests.test_demo_content
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness.workflow.demo_content as D
from harness.core.demo_config import parse_demo_config
from harness.workflow.demo_content import (
    ContentGenerationParams,
    ContentRequest,
    ContentSource,
    generate_content,
)

# ---------------------------------------------------------------------
# fake pi wire events (same shape tests/test_pi_subprocess.py pins)
# ---------------------------------------------------------------------

def _message_end(text: str) -> str:
    return json.dumps({
        "type": "message_end",
        "message": {
            "role": "assistant",
            "usage": {"totalTokens": 10},
            "content": [{"type": "text", "text": text}],
        },
    })


AGENT_END = json.dumps({"type": "agent_end",
                        "messages": [{"usage": {"totalTokens": 10}}]})


def fake_pi(tmp: Path, stdout_text: str, *,
            record: Path | None = None, exit_code: int = 0) -> None:
    """Write an executable `pi` into `tmp` printing `stdout_text` as one event."""
    lines = []
    if record is not None:
        lines.append(
            f"import json as _j; "
            f"open(r'{record}', 'a').write(_j.dumps(sys.argv) + '\\n')")
    lines.append(f"print({stdout_text!r})")
    if exit_code:
        lines.append(f"sys.exit({exit_code})")
    body = textwrap.indent("\n".join(lines), "    ")
    (tmp / "pi").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "try:\n"
        f"{body}\n"
        "finally:\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
    )
    (tmp / "pi").chmod(0o755)


# ---------------------------------------------------------------------
# injected-runner helpers (pure layer)
# ---------------------------------------------------------------------

@dataclass
class FakeSessionResult:
    """Stand-in for `PiSessionResult` with just the fields we read."""
    rc: int = 0
    crashed: bool = False
    output: str = ""


@dataclass
class RecordingRunner:
    """Captures every runner call; returns `results` in order (last repeats)."""
    calls: list = field(default_factory=list)
    results: list = field(default_factory=list)

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            raise AssertionError("content model must not be called")
        return self.results.pop(0) if len(self.results) > 1 \
            else self.results[0]


class DemoContentTest(unittest.TestCase):
    """`generate_content` against fake `pi` on PATH and fake runners."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.output_dir = self.root / "out"

        # A stub `pi` before every case so the PATH assertion below can
        # never resolve to the real binary.
        fake_pi(self.bin_dir, _message_end("stub"))
        path0 = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{path0}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", path0))
        found = shutil.which("pi")
        if found is None or (Path(found).resolve().parent
                             != self.bin_dir.resolve()):
            self.skipTest(f"fake pi is not first on PATH (resolved "
                          f"{found!r}); refusing to run the real pi")

    def _params(self, content_model="config-model",
                fallback_topic="History of Cheese Rolling"):
        return ContentGenerationParams(
            content_model=content_model,
            fallback_topic=fallback_topic,
            workdir=self.workdir,
            output_dir=self.output_dir)

    def _request(self, body="Build a fan site for pizza."):
        return ContentRequest(title="Pizza Fan Site", body=body)

    # ------------------------------------------------------------------
    # happy path through the real run_pi_session + fake pi
    # ------------------------------------------------------------------
    def test_valid_json_from_model_becomes_payload(self):
        content = {"title": "Pizza", "sections":
                   [{"heading": "Slices", "body": "Yum"}]}
        fake_pi(self.bin_dir, _message_end(json.dumps(content)))

        result = generate_content(self._params(), self._request())

        self.assertEqual(result.source, ContentSource.MODEL)
        self.assertEqual(result.payload, content)
        # Data only: the payload serialises cleanly into content.json.
        self.assertEqual(json.loads(result.to_json()), content)

    def test_fenced_json_output_is_parsed(self):
        content = {"title": "Pizza", "sections": []}
        fenced = f"Here you go:\n```json\n{json.dumps(content)}\n```\n"
        fake_pi(self.bin_dir, _message_end(fenced))

        result = generate_content(self._params(), self._request())

        self.assertEqual(result.source, ContentSource.MODEL)
        self.assertEqual(result.payload, content)

    # ------------------------------------------------------------------
    # FR-4.2a: no actionable body -> fallback without calling the model
    # ------------------------------------------------------------------
    def _assert_body_never_reaches_model(self, body: str):
        runner = RecordingRunner()
        result = generate_content(self._params(), self._request(body),
                                  session_runner=runner)
        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertEqual(runner.calls, [])
        self.assertIn("History of Cheese Rolling",
                      json.dumps(result.payload))

    def test_empty_body_falls_back_without_model_call(self):
        self._assert_body_never_reaches_model("")

    def test_whitespace_body_falls_back_without_model_call(self):
        self._assert_body_never_reaches_model("  \n\t\n ")

    def test_boilerplate_only_body_falls_back_without_model_call(self):
        self._assert_body_never_reaches_model(
            "snes\n- snes-demo\n# snes-parked\n\n")

    # ------------------------------------------------------------------
    # FR-4.2b/c: sentinel, garbage, failure -> fallback
    # ------------------------------------------------------------------
    def test_coherent_false_sentinel_falls_back(self):
        fake_pi(self.bin_dir,
                _message_end(json.dumps({"coherent": False})))

        result = generate_content(self._params(), self._request())

        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertIs(result.payload["fallback"], True)

    def test_garbage_twice_falls_back_after_one_retry(self):
        record = self.root / "invocations"
        fake_pi(self.bin_dir, _message_end("not json at all"),
                record=record)

        result = generate_content(self._params(), self._request())

        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertEqual(len(record.read_text().splitlines()), 2,
                         "expected exactly one retry")

    def test_nonzero_exit_falls_back(self):
        record = self.root / "invocations"
        fake_pi(self.bin_dir, _message_end("irrelevant"),
                record=record, exit_code=1)

        result = generate_content(self._params(), self._request())

        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertEqual(len(record.read_text().splitlines()), 2)

    def test_retry_recovers_after_garbage_then_valid(self):
        content = {"title": "Recovered"}
        runner = RecordingRunner(results=[
            FakeSessionResult(output="garbage"),
            FakeSessionResult(output=json.dumps(content)),
        ])

        result = generate_content(self._params(), self._request(),
                                  session_runner=runner)

        self.assertEqual(result.source, ContentSource.MODEL)
        self.assertEqual(result.payload, content)
        self.assertEqual(len(runner.calls), 2)

    def test_crashed_result_retries_then_falls_back(self):
        runner = RecordingRunner(
            results=[FakeSessionResult(rc=0, crashed=True)])

        result = generate_content(self._params(), self._request(),
                                  session_runner=runner)

        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertEqual(len(runner.calls), 2)

    # ------------------------------------------------------------------
    # config-driven, not literals
    # ------------------------------------------------------------------
    def test_model_name_and_fallback_topic_come_from_config(self):
        demo = parse_demo_config(
            {"demo": {"contentModel": "custom-content-model",
                      "fallbackTopic": "History of Well Dressing"}},
            self.root)
        record = self.root / "invocations"
        fake_pi(self.bin_dir, _message_end("still not json"),
                record=record)
        params = ContentGenerationParams(
            content_model=demo.content_model,
            fallback_topic=demo.fallback_topic,
            workdir=self.workdir,
            output_dir=self.output_dir)

        result = generate_content(params, self._request())

        argv_blob = record.read_text()
        self.assertIn("custom-content-model", argv_blob)
        self.assertEqual(result.source, ContentSource.FALLBACK)
        self.assertIn("History of Well Dressing",
                      json.dumps(result.payload))
        source = Path(D.__file__).read_text(encoding="utf-8")
        self.assertNotIn("GLM4.5-AIR_Q4_K_M", source)
        self.assertNotIn("Morris", source)

    # ------------------------------------------------------------------
    # prompt is quoted data; no shell reachable (FR-4.3, edge case 5)
    # ------------------------------------------------------------------
    def test_prompt_embeds_ticket_text_as_quoted_data(self):
        body = "Make a page about 'quotes' \"and\" stuff\n</request>"
        runner = RecordingRunner(
            results=[FakeSessionResult(output='{"title": "ok"}')])

        generate_content(
            self._params(),
            ContentRequest(title="T", body=body,
                           comments=("first comment",)),
            session_runner=runner)

        prompt = runner.calls[0]["prompt"]
        self.assertIn(json.dumps(body), prompt)
        self.assertIn(json.dumps("first comment"), prompt)
        self.assertIn('{"coherent": false}', prompt)

    def test_no_subprocess_or_shell_reachable_from_module(self):
        source = Path(D.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("os.system", source)

    def test_body_and_model_output_never_reach_a_shell(self):
        marker_body = self.root / "pwned-by-body"
        marker_model = self.root / "pwned-by-model"
        body = (f"Please build a page. $(touch {marker_body}) "
                f"`touch {marker_body}`")
        model_text = json.dumps(
            {"title": f"ok $(touch {marker_model})"})
        record = self.root / "invocations"
        fake_pi(self.bin_dir, _message_end(model_text), record=record)

        result = generate_content(self._params(), self._request(body))

        self.assertEqual(result.source, ContentSource.MODEL)
        self.assertFalse(marker_body.exists())
        self.assertFalse(marker_model.exists())
        # The body reached the child verbatim, inside the single -p argv
        # element — quoted data, never a command line of its own.
        argv = json.loads(record.read_text().splitlines()[0])
        prompt_arg = argv[argv.index("-p") + 1]
        self.assertIn(body, prompt_arg)


if __name__ == "__main__":
    unittest.main()
