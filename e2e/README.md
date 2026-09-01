# Containerized End-to-End (E2E) `pi` Test Suite

This directory implements an isolated, container-based end-to-end testing mechanism that **actually invokes `pi`** to implement features in a sandboxed queue and target codebase.

The suite is completely decoupled from the root test suite: running `pytest` in the root repository will discover only the fast unit tests in `tests/` and will never launch containers or model sessions.

---

## Lifecycle Architecture

The test harness follows a 4-step snapshot & ephemeral revert lifecycle:

```
[Current Codebase]
       │
       ▼
1. Build base container image (harness-e2e-base:latest)
       │
       ▼
2. Create ephemeral folder structure & seed target git repo (/workspace/...)
       │
       ▼
3. Commit container state as pristine snapshot (harness-e2e-startpoint:latest)
       │
       ▼
4. For each test in e2e suite:
   ├── Spawn fresh ephemeral container from snapshot
   ├── Inject task into container queue (/workspace/queue/pending/...)
   ├── Execute harness workflow with real pi model sessions
   ├── Inspect and verify code, git merges, tests, and telemetry
   └── Destroy ephemeral container (reverting to clean snapshot)
```

---

## Prerequisites

1. **Podman or Docker** installed and running on the host.
2. **Local Model Server (llama-swap)** or external provider (OpenRouter / Anthropic / OpenAI) running and accessible.

---

## Configuration & Environment Variables

| Variable | Description | Default |
|---|---|---|
| `CONTAINER_ENGINE` | Container runtime (`podman` or `docker`) | Auto-detected |
| `HARNESS_PI_PROVIDER` | Backend provider passed to `pi` | `llama-swap` |
| `PI_E2E_MODEL` | Model for technicalWriter and implementer stages | `qwen2.5-coder:14b` |
| `PI_E2E_ASSESSOR` | Model for specification assessor stage (`ornith`) | `qwen2.5-coder:14b` |
| `KEEP_CONTAINER_ON_FAIL` | Set to `1` to preserve the container on test failure for inspection | `0` |

---

## Invocation

### Run all E2E container tests:
```bash
pytest e2e -v -s
```
or using the helper script:
```bash
./scripts/run-e2e-container.sh
```

### Run a specific test:
```bash
pytest e2e/test_real_pi_pipeline.py -k test_e2e_container_implement_math_feature -v -s
```

### Run with OpenRouter or custom model:
```bash
HARNESS_PI_PROVIDER=openrouter PI_E2E_MODEL="anthropic/claude-3.5-sonnet" pytest e2e -v -s
```
