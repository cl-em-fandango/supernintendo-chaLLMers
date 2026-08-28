# T17 — Drain pi's stderr on a thread and stop pasting it into `output`

**Wave 4** · depends: none (after T01) · finding: F5

## Context
`external/pi_cli.py:78` opens `stderr=subprocess.PIPE` and does not read it until l.121-124 — after
`for line in proc.stdout` ends and after `proc.wait()`. A child that writes more than the ~64 KB OS
pipe buffer to stderr blocks on write, so stdout never closes, so the stdout loop never ends: a
deadlock with no timeout and no output. Separately l.141-142 splices stderr into `output`
(`output += f"\n[stderr]\n{err}"`), and `output` is exactly what `_extract_verdict` scans — so a
model's own stderr text can fabricate a verdict.

## Read first
- `external/pi_cli.py` — `run_pi_session` 32-155 (Popen 73-83, reap 113-124, splice 140-142),
  the heartbeat thread 63-71 for the thread shape to copy, `PiSessionResult` 20-30
- `harness/core/session.py` — where `result.err` (stats notes) and `result.output` (verdict) are used

## Do
1. `PiSessionResult` gains `stderr: str` (default `""`, keep it last so existing construction works).
2. Immediately after `Popen`, start a **daemon** thread that does `for line in proc.stderr:
   stderr_parts.append(line)` — same construction style as the heartbeat thread. Set
   `stop_evt` in the existing `finally` and `join(timeout=2)`.
3. Delete the post-loop `proc.stderr.read()` block. After the reap, set
   `stderr_txt = "".join(stderr_parts)`; `result.stderr = stderr_txt`; keep `err` semantics
   unchanged (error text + last 2000 chars of stderr) so `session.py`'s `[crashed: …]` notes and
   the `pi raw` diagnostic line keep working.
4. Remove `output += f"\n[stderr]\n{err}"`. `output` is assistant text, nothing else. When
   `stderr_txt.strip()` is non-empty, also write it to `out_file.with_suffix(out_file.suffix + ".err")`
   so an operator still gets one file per session side.
5. `out_file.write_text(output)` stays as the last write before the return.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, os, pathlib, tempfile, time, threading, textwrap
threading.Timer(60, lambda: os._exit(1)).start()   # hard guard: a deadlock fails the card
sys.path.insert(0,'.')
fake = pathlib.Path(tempfile.mkdtemp()); (fake/"pi").write_text(textwrap.dedent('''
    #!/usr/bin/env python3
    import sys
    for i in range(4000): print("noisy stderr line %d " % i * 8, file=sys.stderr)
    print('{"type":"message_end","message":{"role":"assistant","usage":{"totalTokens":123},'
          '"content":[{"type":"text","text":"all good VERDICT: done"}]}}')
'''))
(fake/"pi").chmod(0o755); os.environ["PATH"] = f"{fake}:" + os.environ["PATH"]
import external.pi_cli as P
wd = pathlib.Path(tempfile.mkdtemp()); out = wd/"s.out"
t = time.monotonic(); r = P.run_pi_session(model="m", workdir=wd, prompt="p", out_file=out, log=lambda *a: None)
assert time.monotonic() - t < 30, "deadlocked on the stderr pipe"
assert r.rc == 0 and not r.crashed, (r.rc, r.err)
assert "noisy stderr line" in r.stderr and len(r.stderr) > 40000, "stderr not captured"
assert "[stderr]" not in r.output and "noisy" not in r.output, "stderr still spliced into output"
assert "VERDICT: done" in r.output and P._extract_verdict(r.output) == "done"
assert out.exists() and out.with_suffix(".out.err").exists(), "stderr side file missing"
print("stderr drain ok")
PY
```
Must pass, plus the Gate.

## Out of scope
The wall-clock watchdog (T18), the verdict regex and case handling (T19), `session.py`'s
verdict/error mapping (T20), merging stdout and stderr into one stream, log rotation.

## Done when
A child writing 100 KB of stderr cannot block the run; `PiSessionResult.stderr` is populated;
`output` contains no `[stderr]` marker anywhere in the repo (`grep -rn "\[stderr\]" external/` is
empty); a stderr-only `VERDICT:` line can no longer produce a verdict.
