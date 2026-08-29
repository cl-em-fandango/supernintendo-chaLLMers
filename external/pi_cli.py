"""Run pi subprocess and return raw session results.

This module owns the mechanics of talking to pi via subprocess. It does not
handle logging, stats, or policy - just the raw interaction.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

VERDICT_RE = re.compile(r"VERDICT\s*:\s*([A-Za-z_]+)", re.IGNORECASE)
VERDICT_JSON_RE = re.compile(r'"verdict"\s*:\s*"([A-Za-z_]+)"', re.IGNORECASE)

HEARTBEAT_S = 30          # log a heartbeat every 30s while a session runs
HARD_TIMEOUT_S = 5400     # 90 min absolute cap per session
WATCHDOG_GRACE_S = 5      # kill-then-reap grace for the wall-clock watchdog
TERMINATE_GRACE_S = 5     # SIGTERM-then-SIGKILL grace in the shared stop helper
TIMEOUT_ERR_PREFIX = "wall-clock timeout"  # err prefix that marks a timeout exit
OVER_CAP_ERR_PREFIX = "over context cap"   # err prefix that marks an over-cap stop


@dataclass
class PiSessionResult:
    """Raw outcome from a pi subprocess run.

    `output` is assistant text only. The child's stderr is kept separate in
    `stderr` so it can never be scanned as if it were a verdict.

    `over_context_budget` is *not* a crash: it says a streamed usage value went
    strictly over `context_limit` and the session was stopped for that reason.
    The child's own return code stays in `rc` either way, so an over-cap stop
    and a genuine crash remain tellable apart downstream.
    """
    rc: int
    crashed: bool
    err: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path
    stderr: str = ""
    over_context_budget: bool = False
    context_limit: int | None = None


def _terminate_reap(proc: subprocess.Popen, *, grace_s: float = TERMINATE_GRACE_S) -> None:
    """Stop a running child and reap it: SIGTERM first, SIGKILL after `grace_s`.

    One stop path for both reasons we stop early — the wall-clock watchdog and
    the over-context-cap trip — so a session is never torn down two different
    ways. Terminating before killing lets pi close its own streams; the kill is
    only for a child that ignores SIGTERM. Reaping here is what makes the
    caller's later `wait()` return at once instead of blocking on a pipe a live
    child still holds open. A child that already exited is not an error: both
    callers race against a natural exit.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _extract_text_from_message(msg: dict) -> str:
    """Extract all text content from an assistant message regardless of schema."""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                if c.get("type") == "text" and "text" in c:
                    parts.append(str(c["text"]))
                elif "text" in c:
                    parts.append(str(c["text"]))
                elif "content" in c and isinstance(c["content"], str):
                    parts.append(str(c["content"]))
    elif "text" in msg and isinstance(msg["text"], str):
        parts.append(msg["text"])
    return "\n".join(parts)


def _format_token_count(n: int) -> str:
    """Format token count compactly with k suffix if >= 1000."""
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _format_tool_call(e: dict) -> tuple[str, str] | None:
    """Extract (tool_name, summary) from tool call events."""
    t = e.get("type")
    if t not in ("tool_call", "tool_start", "tool_execution"):
        return None

    tool_name = (
        e.get("name")
        or e.get("tool")
        or e.get("tool_name")
        or (e.get("call") or {}).get("name")
        or (e.get("tool_call") or {}).get("name")
        or "unknown"
    )

    args = (
        e.get("input")
        or e.get("args")
        or e.get("arguments")
        or e.get("tool_input")
        or (e.get("call") or {}).get("args")
        or (e.get("tool_call") or {}).get("arguments")
        or {}
    )

    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"raw": args}

    if not isinstance(args, dict):
        args = {"raw": str(args)}

    summary = ""
    if "path" in args or "file" in args or "filename" in args:
        path = args.get("path") or args.get("file") or args.get("filename")
        summary = f'path="{path}"'
        if "edits" in args and isinstance(args["edits"], list):
            summary += f" ({len(args['edits'])} edit{'s' if len(args['edits']) != 1 else ''})"
    elif "command" in args or "cmd" in args:
        cmd = str(args.get("command") or args.get("cmd"))
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        summary = f'command="{cmd}"'
    elif "query" in args:
        summary = f'query="{args["query"]}"'
    else:
        items = [f"{k}={str(v)[:20]!r}" for k, v in list(args.items())[:3]]
        summary = " ".join(items) if items else ""

    return tool_name, summary


def _format_tool_result(e: dict) -> tuple[str, str] | None:
    """Extract (tool_name, summary) from tool result events."""
    t = e.get("type")
    if t not in ("tool_result", "tool_end"):
        return None

    tool_name = (
        e.get("name")
        or e.get("tool")
        or e.get("tool_name")
        or "tool"
    )

    res = (
        e.get("result")
        or e.get("output")
        or e.get("content")
        or e.get("tool_result")
        or ""
    )

    is_err = bool(e.get("is_error") or e.get("error"))

    if isinstance(res, list):
        parts = []
        for p in res:
            if isinstance(p, dict) and "text" in p:
                parts.append(p["text"])
            else:
                parts.append(str(p))
        res = "\n".join(parts)
    elif not isinstance(res, str):
        res = str(res)

    lines = res.strip().splitlines()
    if is_err:
        summary = f"error: {lines[0][:60] if lines else 'failed'}"
    elif len(lines) > 1:
        summary = f"returned {len(lines)} lines"
    elif lines:
        summary = lines[0][:60]
    else:
        summary = "ok"

    return tool_name, summary


def _format_thinking_summary(text: str) -> str | None:
    """Extract a concise one-line summary from assistant thinking/text."""
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.upper().startswith("VERDICT:") or cleaned.lower().startswith("## summary") or cleaned.lower().startswith("# summary"):
        return None
    cleaned = re.sub(r"^[#*\-\s>]+", "", cleaned)
    first_line = cleaned.split("\n")[0].strip()
    if not first_line:
        return None
    if first_line.upper().startswith("VERDICT:") or first_line.lower() == "summary":
        return None
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    return f'"{first_line}"'


def run_pi_session(
    *,
    model: str,
    workdir: str | Path,
    prompt: str,
    out_file: Path,
    log,
    max_context_tokens: int | None = None,
    ui_context: dict | None = None,
) -> PiSessionResult:
    """Run a pi subprocess and return the raw result.
    
    Args:
        model: The model to use
        workdir: Working directory for the subprocess
        prompt: The prompt to send to pi
        out_file: Path where to write the assistant output. Non-empty stderr is
            written alongside it as `<out_file>.err` and never into `out_file`.
        log: Callable for heartbeat logging
        max_context_tokens: Hard context ceiling in tokens, or None for no cap.
            Checked against every streamed usage value, so a session that walks
            past the ceiling is stopped while it is still consuming context
            rather than after it returns. Exactly the cap does not trip.
        
    Returns:
        PiSessionResult with all raw subprocess data: `output` is assistant text,
        `stderr` the child's stderr, `err` the failure text (plus a stderr tail),
        `over_context_budget`/`context_limit` the over-cap trip and the cap that
        was in force.
    """
    workdir = Path(workdir)
    t0 = time.monotonic()
    peak = 0
    text_parts: list[str] = []
    stderr_parts: list[str] = []
    # The drainer can outlive its join below (a grandchild keeping the pipe's
    # write end open), so the list is only ever touched under this lock.
    stderr_lock = threading.Lock()
    rc = 0
    crashed = False
    err = ""
    # Set on the first streamed usage value strictly over `max_context_tokens`.
    # Deliberately not `crashed`: the stop is a budget decision, the child's own
    # exit code stays in `rc`.
    over_context_budget = False

    # heartbeat state (shared with the heartbeat thread)
    hb = {
        "last_event": t0,
        "last_heartbeat": t0,
        "events": 0,
        "peak": 0,
        "current_op": "starting",
    }
    stop_hb = threading.Event()
    stop_stderr = threading.Event()
    stop_watchdog = threading.Event()
    # Set by the watchdog when it kills the child, so the result can tell a
    # wall-clock timeout apart from any other crash.
    killed_by_watchdog = threading.Event()

    spinner_chars = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def heartbeat():
        while not stop_hb.wait(0.15):
            now = time.monotonic()
            idle = now - hb["last_event"]
            if now - hb["last_heartbeat"] >= HEARTBEAT_S:
                hb["last_heartbeat"] = now
                log(f"  … heartbeat: {idle:.0f}s since last event, "
                    f"{hb['events']} events, peak={hb['peak']} tok")

            if hasattr(log, "status") and callable(getattr(log, "status", None)):
                elapsed = now - t0
                spin_idx = int(elapsed / 0.15) % len(spinner_chars)
                sp = spinner_chars[spin_idx]
                mins = int(elapsed // 60)
                secs = int(elapsed % 60)
                time_str = f"{mins:02d}:{secs:02d}"

                ctx_parts = []
                if ui_context:
                    if "stage" in ui_context and ui_context["stage"]:
                        ctx_parts.append(str(ui_context["stage"]).upper())
                    if "slice_id" in ui_context and ui_context["slice_id"]:
                        ctx_parts.append(f"slice {ui_context['slice_id']}")
                prefix = f"[{' '.join(ctx_parts)}] " if ctx_parts else ""

                tok_str = f"peak={_format_token_count(hb['peak'])}"
                if max_context_tokens:
                    pct = (hb['peak'] / max_context_tokens * 100) if max_context_tokens else 0
                    tok_str += f"/{_format_token_count(max_context_tokens)} ({pct:.0f}%)"
                elif ui_context and ui_context.get("budget"):
                    b = ui_context["budget"]
                    pct = (hb['peak'] / b * 100) if b else 0
                    tok_str += f"/{_format_token_count(b)} ({pct:.0f}%)"

                op = hb["current_op"]
                status_line = f"{sp} {prefix}{tok_str} | {op} | {time_str}"
                log.status(status_line)

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # The stderr drainer is started the instant the pipe exists: a child that
    # writes more than the ~64 KB OS pipe buffer to stderr blocks on write until
    # somebody reads, which would otherwise stall stdout, then the reap, forever.
    drain_thread: threading.Thread | None = None

    # Provider is overridable for tests / alternative backends (e.g. openrouter).
    # When empty, unset, or 'none', --provider is omitted so pi resolves models
    # directly by name from configured endpoints (e.g. models-store.json).
    provider = os.environ.get("HARNESS_PI_PROVIDER", "")
    pi_cmd = ["pi"]
    if provider and provider.lower() not in ("none", "null", "disabled", "unset"):
        pi_cmd.extend(["--provider", provider])
    pi_cmd.extend([
        "--model", model,
        "--no-session", "--mode", "json", "-p", prompt,
    ])
    try:
        proc = subprocess.Popen(
            pi_cmd,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None

        def drain_stderr():
            # Read to EOF. `stop_stderr` must never abandon data that is still
            # sitting in the pipe buffer: the child exits, stdout closes and the
            # main thread reaches the finally while hundreds of KB of stderr are
            # still unread.
            for line in proc.stderr:
                if stop_stderr.is_set():
                    return
                with stderr_lock:
                    stderr_parts.append(line)

        drain_thread = threading.Thread(target=drain_stderr, daemon=True)
        drain_thread.start()

        deadline = t0 + HARD_TIMEOUT_S

        def watchdog():
            # A blocked read() yields no line, so the in-loop deadline check below
            # can never fire for a child that prints nothing. This thread stops it
            # on the same clock, and stopping it is what unblocks the read. The
            # sleep is on the stop event (heartbeat shape) so shutdown stays
            # prompt.
            while proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    killed_by_watchdog.set()
                    _terminate_reap(proc)
                    break
                stop_watchdog.wait(min(1.0, remaining))

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

        def measure(usage: dict | None) -> bool:
            """Fold one usage block into `peak`; True when it is over the cap.

            Strictly greater than the cap, so a session that lands exactly on it
            is still inside. The first over-cap value is the trip: the caller
            stops reading stdout immediately, so `err` is written once and can
            never be overwritten by a later, larger value.
            """
            nonlocal peak, over_context_budget, err
            total = int((usage or {}).get("totalTokens", 0))
            peak = max(peak, total)
            hb["peak"] = peak
            if (max_context_tokens is not None and not over_context_budget
                    and total > max_context_tokens):
                over_context_budget = True
                err = (f"{OVER_CAP_ERR_PREFIX}: peak={total} tokens "
                       f"limit={max_context_tokens} tokens")
                return True
            return False

        for line in proc.stdout:
            if time.monotonic() > deadline:
                err = (f"{TIMEOUT_ERR_PREFIX} after {HARD_TIMEOUT_S}s "
                       f"(child still streaming)")
                crashed = True
                break
            line = line.strip()
            if not line:
                continue
            hb["last_event"] = time.monotonic()
            hb["events"] += 1
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                    continue
            t = e.get("type")
            if t in ("tool_call", "tool_start", "tool_execution"):
                info = _format_tool_call(e)
                if info:
                    tool_name, summary = info
                    hb["current_op"] = f"tool: {tool_name} {summary}".strip()
                    log(f"  ⚙ [TOOL:{tool_name}] {summary}")
            elif t in ("tool_result", "tool_end"):
                info = _format_tool_result(e)
                if info:
                    tool_name, summary = info
                    hb["current_op"] = "waiting for LLM"
                    log(f"  ✓ [TOOL:{tool_name}] {summary}")
            elif t == "message_end":
                msg = e.get("message") or {}
                usage = msg.get("usage")
                if measure(usage):
                    break
                if isinstance(usage, dict):
                    in_tok = int(usage.get("inputTokens") or usage.get("prompt_tokens") or 0)
                    out_tok = int(usage.get("outputTokens") or usage.get("completion_tokens") or 0)
                    if in_tok or out_tok:
                        tot_tok = int(usage.get("totalTokens") or usage.get("total_tokens") or (in_tok + out_tok))
                        log(f"  • [LLM] in={_format_token_count(in_tok)} out={_format_token_count(out_tok)} tok (turn total={_format_token_count(tot_tok)}, peak={_format_token_count(peak)})")
                if msg.get("role") == "assistant":
                    content_text = _extract_text_from_message(msg)
                    if content_text:
                        text_parts.append(content_text)
                        summary = _format_thinking_summary(content_text)
                        if summary:
                            log(f"  • [THINK] {summary}")
            elif t == "agent_end":
                for m in e.get("messages", []):
                    if measure((m or {}).get("usage")):
                        break
                    if not text_parts and isinstance(m, dict) and m.get("role") == "assistant":
                        txt = _extract_text_from_message(m)
                        if txt:
                            text_parts.append(txt)
                if over_context_budget:
                    break
        # stdout closed (or we broke). Reap the process.
        if over_context_budget:
            # Stop the child *before* the wait. A session that is over cap is by
            # definition still working, so it still holds the stdout pipe open:
            # waiting first would block on output we have decided not to read.
            _terminate_reap(proc)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            crashed = True
            err = err or "pi did not exit after stdout closed"
        rc = proc.returncode
        if rc != 0 and not err:
            err = f"pi exited rc={rc}"
            crashed = True
    except FileNotFoundError as e:
        rc, err, crashed = 127, f"failed to spawn pi: {e}", True
    finally:
        stop_hb.set()
        hb_thread.join(timeout=2)
        if hasattr(log, "clear_status") and callable(getattr(log, "clear_status", None)):
            log.clear_status()
        stop_watchdog.set()
        watchdog_thread.join(timeout=WATCHDOG_GRACE_S)
        # The child is reaped by now, so its stderr is at EOF and this join
        # returns as soon as the buffer is drained. The stop event is only the
        # safety valve for a wedged child that keeps the pipe open past the
        # join, so it is set *after* the join - setting it first drops the tail.
        if drain_thread is not None:
            drain_thread.join(timeout=2)
        stop_stderr.set()

    duration = time.monotonic() - t0
    if killed_by_watchdog.is_set():
        crashed = True
        if not err.startswith(TIMEOUT_ERR_PREFIX):
            err = (f"{TIMEOUT_ERR_PREFIX} after {HARD_TIMEOUT_S}s"
                   + (f": {err}" if err else ""))
    with stderr_lock:
        stderr_txt = "".join(stderr_parts)
    if stderr_txt.strip():
        err = (err + "\n" if err else "") + stderr_txt.strip()[-2000:]
    output = "\n".join(text_parts)
    if stderr_txt.strip():
        # Operator-side copy of stderr, kept off `output` (which is what the
        # verdict extractor reads) but still recoverable as one file per session.
        out_file.with_suffix(out_file.suffix + ".err").write_text(stderr_txt)
    out_file.write_text(output)

    # Diagnostic: log the raw event tally + output size so an empty/zero-token
    # session is visible (peak=0 + verdict=unknown means no message_end/agent_end
    # events and no text came back from pi).
    log(f"  … pi raw: events={hb['events']} peak={peak} tok "
        f"output={len(output)} chars rc={rc} crashed={crashed} "
        f"stderr={len(stderr_txt)} chars")
    if peak == 0 and not output.strip():
        log(f"  … pi EMPTY: no tokens and no text returned "
            f"(rc={rc}, crashed={crashed}, stderr={err[:200]!r})")
    if over_context_budget:
        log(f"  … pi {err} (rc={rc}, output={len(output)} chars)")

    return PiSessionResult(
        rc=rc,
        crashed=crashed,
        err=err,
        peak_tokens=peak,
        duration_s=duration,
        output=output,
        out_file=out_file,
        stderr=stderr_txt,
        over_context_budget=over_context_budget,
        context_limit=max_context_tokens,
    )


def _extract_verdict(output: str) -> str:
    """Extract a verdict from assistant text.

    The wire format may be any case (`VERDICT: DONE`), so the match is
    case-insensitive and the captured group is lowercased to land on a
    `Verdict` enum value. When a run emits several verdict lines the last one
    wins: a session that re-checks its work and changes its mind is normal.
    Falls back to a JSON `"verdict": "..."` field, then to "unknown".
    """
    matches = VERDICT_RE.findall(output)
    if matches:
        return matches[-1].strip().lower()
    j = VERDICT_JSON_RE.findall(output)
    if j:
        return j[-1].strip().lower()
    return "unknown"


def _now() -> str:
    """Get current timestamp in ISO format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")