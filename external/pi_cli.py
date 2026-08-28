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
TIMEOUT_ERR_PREFIX = "wall-clock timeout"  # err prefix that marks a timeout exit


@dataclass
class PiSessionResult:
    """Raw outcome from a pi subprocess run.

    `output` is assistant text only. The child's stderr is kept separate in
    `stderr` so it can never be scanned as if it were a verdict.
    """
    rc: int
    crashed: bool
    err: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path
    stderr: str = ""


def run_pi_session(
    *,
    model: str,
    workdir: str | Path,
    prompt: str,
    out_file: Path,
    log,
) -> PiSessionResult:
    """Run a pi subprocess and return the raw result.
    
    Args:
        model: The model to use
        workdir: Working directory for the subprocess
        prompt: The prompt to send to pi
        out_file: Path where to write the assistant output. Non-empty stderr is
            written alongside it as `<out_file>.err` and never into `out_file`.
        log: Callable for heartbeat logging
        
    Returns:
        PiSessionResult with all raw subprocess data: `output` is assistant text,
        `stderr` the child's stderr, `err` the failure text (plus a stderr tail)
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

    # heartbeat state (shared with the heartbeat thread)
    hb = {"last_event": t0, "events": 0, "peak": 0}
    stop_hb = threading.Event()
    stop_stderr = threading.Event()
    stop_watchdog = threading.Event()
    # Set by the watchdog when it kills the child, so the result can tell a
    # wall-clock timeout apart from any other crash.
    killed_by_watchdog = threading.Event()

    def heartbeat():
        while not stop_hb.wait(HEARTBEAT_S):
            idle = time.monotonic() - hb["last_event"]
            log(f"  … heartbeat: {idle:.0f}s since last event, "
                 f"{hb['events']} events, peak={hb['peak']} tok")

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # The stderr drainer is started the instant the pipe exists: a child that
    # writes more than the ~64 KB OS pipe buffer to stderr blocks on write until
    # somebody reads, which would otherwise stall stdout, then the reap, forever.
    drain_thread: threading.Thread | None = None

    # Provider is overridable for tests / alternative backends (e.g. openrouter);
    # production default stays llama-swap.
    provider = os.environ.get("HARNESS_PI_PROVIDER", "llama-swap")
    try:
        proc = subprocess.Popen(
            [
                "pi", "--provider", provider, "--model", model,
                "--no-session", "--mode", "json", "-p", prompt,
            ],
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
            # can never fire for a child that prints nothing. This thread kills it
            # on the same clock, and the kill is what unblocks the read. The sleep
            # is on the stop event (heartbeat shape) so shutdown stays prompt.
            while proc.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    killed_by_watchdog.set()
                    proc.kill()
                    break
                stop_watchdog.wait(min(1.0, remaining))

        watchdog_thread = threading.Thread(target=watchdog, daemon=True)
        watchdog_thread.start()

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
            t = e.get("type")
            if t == "message_end":
                u = (e.get("message") or {}).get("usage") or {}
                peak = max(peak, int(u.get("totalTokens", 0)))
                hb["peak"] = peak
                msg = e.get("message") or {}
                if msg.get("role") == "assistant":
                    for c in msg.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
            elif t == "agent_end":
                for m in e.get("messages", []):
                    u = (m or {}).get("usage") or {}
                    peak = max(peak, int(u.get("totalTokens", 0)))
                    hb["peak"] = peak
        # stdout closed (or we broke). Reap the process.
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

    return PiSessionResult(
        rc=rc,
        crashed=crashed,
        err=err,
        peak_tokens=peak,
        duration_s=duration,
        output=output,
        out_file=out_file,
        stderr=stderr_txt,
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