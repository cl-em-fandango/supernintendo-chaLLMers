# Handover Note: Harness Sandbox & Task Execution Readiness

**Date:** 2026-08-27  
**Branch:** `pi/trunk` (Clean working tree)  
**Location:** `/home/donald/work/harness`

---

## 1. What Was Accomplished in This Session

1. **Analysis & Refinement of Claimed Feature Queue (`/home/donald/work/queue/claimed`):**
   - Refined and critiqued tickets with usefulness, feasibility, completeness, and T-shirt size effort:
     - `003-keep-rejected-features-for-postereity.md` (Size: **S**)
     - `004-model-refresh.md` (Size: **M**)
     - `005-stats-for-rejected-auto-ideas-not-showing.md` (Size: **S**)
     - `007-sandbox-for-harness-with-appropriate-firewall-configuration.md` (Size: **M**)
     - `008-coding-standards.md` (Size: **S**)
     - `auto-3-...md` (Artifact-based proposal handoff) (Size: **M**)
   - Removed stale fragment `auto-4-...md`.

2. **Active Task `002-pipeline-checkpoint-and-resume` Audit & Cleanup:**
   - Identified that the core engine for checkpointing/resuming (`task.json` state, `process()` resume logic, `harness.py resume`, `--continue` / `--fresh` flags) was already implemented, committed, and passing all 40 unit tests on `pi/trunk`.
   - Moved the completed active directory `/home/donald/work/queue/active/002-pipeline-checkpoint-and-resume` to `/home/donald/work/queue/done/`.
   - Created completion summary record in `/home/donald/work/queue/review/002-pipeline-checkpoint-and-resume.md`.
   - Confirmed remaining edge cases (per-slice checkpoints, merge checkpoints) are formally carded in `plan-2026-08-26/` (`T26`, `T27`/`T70`/`T71`, `T54`, `T59`).
   - `queue/active/` is now completely clean and ready for new task claims.

3. **Frozen Sandbox Container & Execution Scripts:**
   - `docker/Dockerfile`: Builds a lightweight container (`registry.fedoraproject.org/fedora-minimal:latest`) freezing harness code in `/opt/harness-frozen`.
   - `docker/init-firewall.sh` & `docker/entrypoint.sh`: Restricts container outbound traffic (loopback, DNS, local LLM bridge gateway, HTTP/HTTPS for packages, SSH).
   - `scripts/rebuild-container.sh`: Podman/Docker rebuild script tagging `harness-sandbox:frozen-latest`.
   - `scripts/run-sandbox.sh`: Wrapper mounting target workspace and running commands inside container.
   - **Tested and verified:** Status and full 40-test test suite run cleanly inside the container against the host workspace.

---

## 2. Important Findings & Notes for the Next Session

- **`config.json` Path Dependency:**  
  `harness/composition.py` defaults to reading `config.json` from the repository root, where `"workDir": "/home/donald/work"`. When executing `run-sandbox.sh` on arbitrary custom directories, ensure the passed directory contains its own `config.json` with `"workDir"` pointing to that directory, or pass `HARNESS_CONFIG` / explicit config path.
- **Firewall Capabilities:**  
  Container uses `--cap-add=NET_ADMIN` and `--cap-add=NET_RAW` to allow `iptables` rules setup inside the container user namespace without needing root on the host.

---

## 3. Quickstart for Next Session

```bash
cd /home/donald/work/harness

# 1. Verify container image is built (or rebuild if code changed)
./scripts/rebuild-container.sh

# 2. Verify status and test suite via container
./scripts/run-sandbox.sh /home/donald/work python3 /opt/harness-frozen/harness.py status
./scripts/run-sandbox.sh /home/donald/work python3 -m unittest discover -s /opt/harness-frozen/tests

# 3. Start task execution (or run supervisor inside sandbox)
./scripts/run-sandbox.sh /home/donald/work python3 /opt/harness-frozen/harness.py run
```
