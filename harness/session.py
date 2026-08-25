"""Run one fresh, token-budgeted pi session and record stats.

Uses `pi --mode json --no-session` so we can stream usage events and compute
peak context tokens without persisting a session file.

Resilience:
- A heartbeat thread logs every N seconds so a hung session is visible, not silent.
- If the pi process dies (crash/OOM) we detect it, record the failure, and return
  a non-ok result so the calling stage can retry. Artifacts the model already
  wrote to disk are preserved, so a retry continues rather than starting over.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .stats import SessionRecord, StatsStore

HEARTBEAT_S = 30          # log a heartbeat every 30s while a session runs
HARD_TIMEOUT_S = 5400     # 90 min absolute cap per session


@dataclass
class SessionResult:
    ok: bool
    verdict: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path
    crashed: bool = False


class SessionRunner:
    def __init__(self, cfg: Config, store: StatsStore, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log

    def run(
        self,
        model: str,
        workdir: str | Path,
        prompt: str,
        *,
        task_id: str | None = None,
        stage: str = "unknown",
        slice_id: str | None = None,
        iteration: int = 1,
        notes: str = "",
    ) -> SessionResult:
        workdir = Path(workdir)
        out_file = workdir / f".pi-session-{stage}-{int(time.time())}.out"

        self.log(f"  ▶ {stage} model={model} iter={iteration} budget={self.cfg.token_budget}k")
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
                self.log(f"  … {stage} heartbeat: {idle:.0f}s since last event, "
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

        verdict = _extract_verdict(output)
        if crashed and verdict == "unknown":
            verdict = "error"
        self.store.record(SessionRecord(
            ts=_now(),
            task_id=task_id,
            stage=stage,
            model=model,
            verdict=verdict,
            outcome=_outcome(verdict),
            peak_tokens=peak,
            duration_s=round(duration, 1),
            rc=rc,
            prompt_chars=len(prompt),
            slice=slice_id,
            iteration=iteration,
            notes=notes + (f" [crashed: {err[:120]}]" if crashed else ""),
        ))
        self.log(f"  ◀ {stage} rc={rc} tokens={peak} verdict={verdict} "
                 f"crashed={crashed} ({duration:.0f}s)")

        return SessionResult(
            ok=rc == 0 and not crashed,
            verdict=verdict,
            peak_tokens=peak,
            duration_s=duration,
            output=output,
            out_file=out_file,
            crashed=crashed,
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _extract_verdict(output: str) -> str:
    matches = re.findall(r"VERDICT:\s*([a-z_]+)", output)
    if matches:
        return matches[-1]
    j = re.findall(r'"verdict"\s*:\s*"([a-z_]+)"', output)
    if j:
        return j[-1]
    return "unknown"


def _outcome(verdict: str) -> str:
    return verdict if verdict in (
        "pass", "fail", "kickback", "kickout", "done",
        "progress", "resliced", "error",
    ) else "unknown"
