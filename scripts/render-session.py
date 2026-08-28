#!/usr/bin/env python3
"""render-session.py — make a `pi --mode json` session stream readable.

pi's json mode writes one event per line, and almost all of them are token
deltas (`thinking_delta`, `toolcall_delta`, `tool_execution_update`): a
15-minute card run is ~10k lines of single-word fragments, which is what the
terminal fills with when `scripts/implement-dir.sh` runs without this filter.

This drops the deltas and prints what actually happened — one line per tool
call, a short excerpt of each tool result, assistant text in full, and a
footer with the totals plus whether the stream ended cleanly or was cut off
mid-generation.

Reads json lines on stdin, writes text to stdout, flushes per event so it can
sit at the end of a live pipe. Lines that are not json (the child's stderr,
`timeout`'s complaints) are passed through with a `[?]` prefix so nothing is
lost. Unknown event types are counted, not printed. Line timestamps are the
clock as it ticks — replaying an old log stamps lines with replay time, the
footer duration is the session's own.

Env:
  PI_RENDER_THINKING=1  also print a one-line preview of each thinking block
  PI_RENDER_CHARS=N     width of the tool-result / argument excerpt (default 300)
  PI_RENDER_LINES=N     how many lines of each tool result to show (default 2)
  PI_RENDER_FULL=1      no truncation anywhere

Replay an old run: scripts/render-session.py < .pi-implement-dir/T06-….out
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


THINKING = os.environ.get("PI_RENDER_THINKING", "") == "1"
FULL = os.environ.get("PI_RENDER_FULL", "") == "1"
CHARS = _int("PI_RENDER_CHARS", 300)
LINES = _int("PI_RENDER_LINES", 2)

# Events that carry nothing a human is missing (deltas, or the same tool call
# and message already rendered from the `*_end` events).
NOISE = {"message_update", "tool_execution_update", "tool_execution_start",
         "turn_end", "agent_start", "agent_settled"}


def clip(text: str, limit: int = CHARS) -> str:
    """One line, at most `limit` chars (unless PI_RENDER_FULL=1)."""
    text = " ".join(str(text).split())
    if FULL or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f" … (+{len(text) - limit} chars)"


def describe_args(name: str, args: object) -> str:
    """A one-line handle on a tool call, per tool."""
    if not isinstance(args, dict):
        return clip(json.dumps(args, default=str), 200)
    if name == "bash":
        return clip(str(args.get("command", "")), 200)
    if name == "read":
        out = str(args.get("path", "?"))
        if "offset" in args or "limit" in args:
            out += f" [lines {args.get('offset', '?')}..{args.get('limit', '?')}]"
        return out
    if name == "write":
        body = str(args.get("content", ""))
        return (f"{args.get('path', '?')} "
                f"({len(body)} bytes, {body.count(chr(10)) + 1} lines)")
    if name == "edit":
        edits = args.get("edits")
        n = len(edits) if isinstance(edits, list) else 0
        return f"{args.get('path', '?')} ({n} replacement{'s' if n != 1 else ''})"
    return clip(json.dumps(args, default=str), 200)


def result_text(result: object) -> str:
    if not isinstance(result, dict):
        return "" if result is None else str(result)
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, dict):
            parts.append(f"<{block.get('type')}>")
        else:
            parts.append(str(block))
    return "\n".join(parts)


class Out:
    """Counters + printing, so the footer can report how the run went."""

    def __init__(self) -> None:
        self.stream = sys.stdout
        self.calls = 0
        self.errors = 0
        self.turns = 0
        self.texts = 0
        self.thoughts = 0
        self.tokens = 0
        self.unparsed = 0
        self.unknown: dict[str, int] = {}
        self.rendered_calls: set[str] = set()
        self.rendered_text: set[str] = set()
        self.started = time.time()
        self.session_start: float | None = None
        self.last_event: float | None = None
        self.clean_end = False
        self.will_retry = False

    def line(self, text: str) -> None:
        self.stream.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.stream.flush()

    def tool_call(self, name: str, args: object, call_id: str | None) -> None:
        if call_id and call_id in self.rendered_calls:
            return
        if call_id:
            self.rendered_calls.add(call_id)
        self.calls += 1
        self.line(f"→ {name:<6} {describe_args(name, args)}")

    def tool_result(self, name: str, result: object, is_error: bool) -> None:
        text = result_text(result)
        body = [ln for ln in text.splitlines() if ln.strip()]
        shown = body[:LINES] if not FULL else body
        size = f"{len(text)} B, {len(body)} line{'s' if len(body) != 1 else ''}"
        mark = "✗ ERROR" if is_error else "←"
        if is_error:
            self.errors += 1
        if shown:
            self.line(f"   {mark} {name} {size}: " + " ⏎ ".join(clip(s) for s in shown))
        else:
            self.line(f"   {mark} {name} {size}")
        if len(body) > len(shown):
            self.line(f"     … (+{len(body) - len(shown)} lines)")

    def assistant_text(self, text: str) -> None:
        lines = text.strip("\n").rstrip().splitlines()
        if not lines or text in self.rendered_text:
            return
        self.rendered_text.add(text)
        self.texts += 1
        for ln in lines:
            self.line(f"💬 {ln}")

    def thinking(self, text: str) -> None:
        if not text.strip():
            return
        self.thoughts += 1
        if THINKING:
            self.line(f"🧠 {clip(text)}")

    def add_usage(self, usage: object) -> None:
        if isinstance(usage, dict):
            self.tokens += int(usage.get("totalTokens") or 0)

    def note_time(self, stamp: object) -> None:
        """Track the session's own clock (ms epoch on message events)."""
        if isinstance(stamp, (int, float)):
            self.last_event = max(self.last_event or 0.0, stamp / 1000)

    def footer(self) -> None:
        span = time.time() - self.started
        if self.session_start and self.last_event and self.last_event > self.session_start:
            span = self.last_event - self.session_start
        mins, secs = divmod(int(span), 60)
        dur = f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m{secs:02d}s"
        self.line(f"■ {self.turns} turn{'s' if self.turns != 1 else ''} · "
                  f"{self.calls} tool call{'s' if self.calls != 1 else ''}"
                  f" ({self.errors} error{'s' if self.errors != 1 else ''}) · "
                  f"{self.thoughts} thinking block{'s' if self.thoughts != 1 else ''} · "
                  f"~{self.tokens / 1000:.1f}k tok · {dur}")
        if not self.clean_end:
            self.line("⚠ stream ended without agent_end — the session was cut off "
                      "(Ctrl-C, `timeout`, provider drop). Work is whatever the last "
                      "lines show; nothing committed itself.")
        elif self.will_retry:
            self.line("⚠ agent_end willRetry=true")
        if self.unparsed:
            self.line(f"ℹ {self.unparsed} non-json line(s) passed through above")


def handle(event: dict, out: Out) -> None:
    kind = event.get("type")
    if kind == "session":
        stamp = event.get("timestamp")
        if isinstance(stamp, str):
            try:
                out.session_start = datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        out.line(f"# session {str(event.get('id', ''))[:8]} · {event.get('cwd', '?')} "
                 f"· json v{event.get('version', '?')}")
    elif kind == "turn_start":
        out.turns += 1
    elif kind == "message_start":
        msg = event.get("message") or {}
        out.note_time(msg.get("timestamp"))
        if msg.get("role") == "user":
            body = " ".join(b.get("text", "") for b in msg.get("content") or []
                            if isinstance(b, dict) and b.get("type") == "text")
            out.line(f"▶ prompt {clip(body, 200)}")
    elif kind == "message_update":
        ev = event.get("assistantMessageEvent") or {}
        shape = ev.get("type")
        if shape == "toolcall_end":
            call = ev.get("toolCall") or {}
            out.tool_call(call.get("name", "?"), call.get("arguments"), call.get("id"))
        elif shape == "text_end":
            out.assistant_text(ev.get("content") or "")
        elif shape == "thinking_end":
            out.thinking(ev.get("content") or "")
    elif kind == "message_end":
        msg = event.get("message") or {}
        out.add_usage(msg.get("usage"))
        out.note_time(msg.get("timestamp"))
        if msg.get("role") != "assistant":
            return
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                out.assistant_text(block.get("text") or "")
            elif block.get("type") == "toolCall":
                out.tool_call(block.get("name", "?"), block.get("arguments"),
                              block.get("id"))
    elif kind == "tool_execution_end":
        out.tool_result(event.get("toolName", "?"), event.get("result"),
                        bool(event.get("isError")))
    elif kind == "agent_end":
        out.clean_end = True
        out.will_retry = bool(event.get("willRetry"))
    elif kind not in NOISE:
        out.unknown[kind] = out.unknown.get(kind, 0) + 1


def main() -> int:
    try:                                  # `| head` must not print a traceback
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):  # not unix, or not the main thread
        pass
    out = Out()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("{"):
            out.line(f"[?] {line}")
            out.unparsed += 1
            continue
        try:
            event = json.loads(line)
        except ValueError:
            out.line(f"[?] {clip(line, 200)}")
            out.unparsed += 1
            continue
        try:
            handle(event, out)
        except Exception as exc:                     # never break the run
            out.line(f"[?] unrenderable {event.get('type')}: {exc}")
    if out.unknown:
        out.line("ℹ unhandled events: " + ", ".join(
            f"{k}×{v}" for k, v in sorted(out.unknown.items())))
    out.footer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
