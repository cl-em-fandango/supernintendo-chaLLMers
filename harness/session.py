"""Run one fresh, token-budgeted pi session and record stats.

Uses `pi --mode json --no-session` so we can stream usage events and compute
peak context tokens without persisting a session file.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .stats import SessionRecord, StatsStore


@dataclass
class SessionResult:
    ok: bool
    verdict: str
    peak_tokens: int
    duration_s: float
    output: str
    out_file: Path


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
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = e.get("type")
                if t == "message_end":
                    u = (e.get("message") or {}).get("usage") or {}
                    peak = max(peak, int(u.get("totalTokens", 0)))
                    msg = e.get("message") or {}
                    if msg.get("role") == "assistant":
                        for c in msg.get("content", []):
                            if isinstance(c, dict) and c.get("type") == "text":
                                text_parts.append(c.get("text", ""))
                elif t == "agent_end":
                    for m in e.get("messages", []):
                        u = (m or {}).get("usage") or {}
                        peak = max(peak, int(u.get("totalTokens", 0)))
            proc.wait(timeout=60)
            rc = proc.returncode
            err = proc.stderr.read() if proc.stderr else ""
        except subprocess.TimeoutExpired:
            rc = 124
            err = "session timed out"
            try:
                proc.kill()
            except Exception:
                pass
        except FileNotFoundError as e:
            rc = 127
            err = f"failed to spawn pi: {e}"
        else:
            err = ""
        duration = time.monotonic() - t0

        output = "\n".join(text_parts)
        if err:
            output += f"\n[stderr]\n{err}"
        out_file.write_text(output)

        verdict = _extract_verdict(output)
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
            notes=notes,
        ))
        self.log(f"  ◀ {stage} rc={rc} tokens={peak} verdict={verdict} ({duration:.0f}s)")

        return SessionResult(
            ok=rc == 0,
            verdict=verdict,
            peak_tokens=peak,
            duration_s=duration,
            output=output,
            out_file=out_file,
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
