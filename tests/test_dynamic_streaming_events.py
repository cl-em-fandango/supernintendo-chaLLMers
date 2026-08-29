"""Tests for stream-first CLI dynamic events and tool / thinking parser."""
from __future__ import annotations

import unittest

import external.pi_cli as P


class DynamicStreamingEventsTest(unittest.TestCase):
    def test_format_token_count(self):
        self.assertEqual(P._format_token_count(0), "0")
        self.assertEqual(P._format_token_count(999), "999")
        self.assertEqual(P._format_token_count(1000), "1.0k")
        self.assertEqual(P._format_token_count(15400), "15.4k")

    def test_format_tool_call_read(self):
        e = {
            "type": "tool_call",
            "name": "read",
            "arguments": {"path": "harness/core/session.py"},
        }
        res = P._format_tool_call(e)
        self.assertIsNotNone(res)
        name, summary = res
        self.assertEqual(name, "read")
        self.assertEqual(summary, 'path="harness/core/session.py"')

    def test_format_tool_call_edit(self):
        e = {
            "type": "tool_start",
            "name": "edit",
            "input": {"path": "foo.py", "edits": [{"old": "a", "new": "b"}]},
        }
        res = P._format_tool_call(e)
        self.assertIsNotNone(res)
        name, summary = res
        self.assertEqual(name, "edit")
        self.assertEqual(summary, 'path="foo.py" (1 edit)')

    def test_format_tool_call_bash(self):
        e = {
            "type": "tool_execution",
            "tool_name": "bash",
            "tool_input": {"command": "python3 -m unittest discover -s tests"},
        }
        res = P._format_tool_call(e)
        self.assertIsNotNone(res)
        name, summary = res
        self.assertEqual(name, "bash")
        self.assertEqual(summary, 'command="python3 -m unittest discover -s tests"')

    def test_format_tool_result_multiline(self):
        e = {
            "type": "tool_result",
            "name": "read",
            "result": "line1\nline2\nline3\nline4\n",
        }
        res = P._format_tool_result(e)
        self.assertIsNotNone(res)
        name, summary = res
        self.assertEqual(name, "read")
        self.assertEqual(summary, "returned 4 lines")

    def test_format_tool_result_error(self):
        e = {
            "type": "tool_result",
            "name": "bash",
            "is_error": True,
            "result": "Command exited with code 1\nTraceback...",
        }
        res = P._format_tool_result(e)
        self.assertIsNotNone(res)
        name, summary = res
        self.assertEqual(name, "bash")
        self.assertTrue(summary.startswith("error: Command exited with code 1"))

    def test_format_thinking_summary(self):
        txt = "### Step 1\nI will first read the test suite to inspect existing behavior."
        res = P._format_thinking_summary(txt)
        self.assertEqual(res, '"Step 1"')

        txt2 = "Let's inspect the math utility requirements and check edge cases."
        res2 = P._format_thinking_summary(txt2)
        self.assertEqual(res2, '"Let\'s inspect the math utility requirements and check edge cases."')

        # Verdict or summary headers are ignored
        self.assertIsNone(P._format_thinking_summary("VERDICT: DONE"))
        self.assertIsNone(P._format_thinking_summary("## Summary\nAll completed."))


if __name__ == "__main__":
    unittest.main()
