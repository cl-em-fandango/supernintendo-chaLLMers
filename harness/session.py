"""Run one fresh, token-budgeted pi session and record stats."""
from __future__ import annotations

import json
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
    session_file: str | None = None


class SessionRunner:
    def __init__(self, cfg: Config, store: StatsStore, log=print):
        self.cfg = cfg
        self.store = store
        self.log = log
        cfg.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _existing_sessions(self) -> set[str]:
        out = set()
        for sub in self.cfg.sessions_dir.iterdir():
            if sub.is_dir():
                out.update(str(p) for p in sub.glob("*.jsonl"))
        return out

    def _peak_tokens(self, session_file: str | None) -> int:
        if not session_file:
            return 0
        peak = 0
        try:
            for line in Path(session_file).read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    u = (e.get("message") or {}).get("usage") or {}
                    peak = max(peak, int(u.get("totalTokens", 0)))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
        except OSError:
            pass
        return peak

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
        before = self._existing_sessions()

        self.log(f"  ▶ {stage} model={model} iter={iteration} budget={self.cfg.token_budget}k")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                [
                    "pi", "--provider", "llama-swap", "--model", model,
                    "--session-dir", str(self.cfg.sessions_dir),
                    "--no-session", "-p", prompt,
                ],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=3600,
            )
            rc, output = proc.returncode, proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        except subprocess.TimeoutExpired:
            rc, output = 124, "session timed out after 3600s"
        except FileNotFoundError as e:
            rc, output = 127, f"failed to spawn pi: {e}"
        duration = time.monotonic() - t0

        out_file.write_text(output)

        # locate the session file this run created
        session_file = None
        new = set(self._existing_sessions()) - before
        if new:
            session_file = max(new, key=lambda p: Path(p).stat().st_mtime)

        peak = self._peak_tokens(session_file)
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
            session_file=session_file,
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
            session_file=session_file,
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _extract_verdict(output: str) -> str:
    import re
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
