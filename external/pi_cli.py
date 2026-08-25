"""Run pi subprocess and return raw session results.

This module owns the mechanics of talking to pi via subprocess. It does not
handle logging, stats, or policy - just the raw interaction.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HEARTBEAT_S = 30          # log a heartbeat every 30s while a session runs
HARD_TIMEOUT_S = 5400     # 90 min absolute cap per session


@dataclass
class PiSessionResult:
    """Raw outcome from a pi subprocess run."""
    rc: int
    crashed: bool
    err: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path


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
        out_file: Path where to write the output
        log: Callable for heartbeat logging
        
    Returns:
        PiSessionResult with all raw subprocess data
    """
    workdir = Path(workdir)
    t0 = time.monotonic()
    peak = 0
    text_parts: list[str] = []
    rc = 0
    crashed = False
    err = ""

    # heartbeat state (shared with the watchdog thread)
    hb = {"last_event": t0, "events": 0, "peak": 0}
    stop_hb = threading.Event()

    def heartbeat():
        while not stop_hb.wait(HEARTBEAT_S):
            idle = time.monotonic() - hb["last_event"]
            log(f"  … heartbeat: {idle:.0f}s since last event, "
                 f"{hb['events']} events, peak={hb['peak']} tok")

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    try:
        proc = subprocess.Popen(
            [
                "pi", "--provider", "llama-swap", "--model", model,
                "--no-session", "--mode", "json", "-p", prompt,
            ],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        deadline = t0 + HARD_TIMEOUT_S
        for line in proc.stdout:
            if time.monotonic() > deadline:
                err = f"hard timeout after {HARD_TIMEOUT_S}s"
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
        if proc.stderr:
            stderr_txt = proc.stderr.read()
            if stderr_txt.strip():
                err = (err + "\n" if err else "") + stderr_txt.strip()[-2000:]
    except FileNotFoundError as e:
        rc, err, crashed = 127, f"failed to spawn pi: {e}", True
    finally:
        stop_hb.set()
        hb_thread.join(timeout=2)

    duration = time.monotonic() - t0
    output = "\n".join(text_parts)
    if err:
        output += f"\n[stderr]\n{err}"
    out_file.write_text(output)

    return PiSessionResult(
        rc=rc,
        crashed=crashed,
        err=err,
        peak_tokens=peak,
        duration_s=duration,
        output=output,
        out_file=out_file,
    )


def _extract_verdict(output: str) -> str:
    """Extract verdict from pi output."""
    matches = re.findall(r"VERDICT:\s*([a-z_]+)", output)
    if matches:
        return matches[-1]
    j = re.findall(r'"verdict"\s*:\s*"([a-z_]+)"', output)
    if j:
        return j[-1]
    return "unknown"


def _now() -> str:
    """Get current timestamp in ISO format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")