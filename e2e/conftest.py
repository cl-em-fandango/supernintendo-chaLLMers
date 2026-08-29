"""Pytest fixtures for containerized end-to-end testing with real pi subprocess invocations."""
from __future__ import annotations

import os
from typing import Generator

import pytest

from .container_driver import (
    BASE_IMAGE_TAG,
    DEFAULT_ASSESSOR_MODEL,
    DEFAULT_IMP_MODEL,
    DEFAULT_TW_MODEL,
    STARTPOINT_IMAGE_TAG,
    ContainerLifecycleManager,
    EphemeralContainer,
    get_container_engine,
)


@pytest.fixture(scope="session")
def container_engine() -> str:
    """Ensure podman or docker is available."""
    try:
        return get_container_engine()
    except RuntimeError as e:
        pytest.skip(f"Container engine not available: {e}")


@pytest.fixture(scope="session")
def e2e_container_snapshot(container_engine: str) -> str:
    """Session fixture: Builds the base image, sets up the workspace structure, and commits snapshot.

    Step 1: Create container and add current version of source.
    Step 2: Create folder structure in container and copy agent config to ~/.pi/agent.
    Step 3: Save container snapshot as 'start point' for each test in the e2e suite.
    """
    manager = ContainerLifecycleManager(engine=container_engine)

    # 1. Build base image with latest source
    manager.build_base_image(BASE_IMAGE_TAG)

    # 2 & 3. Create folder structure in container and save snapshot startpoint
    manager.create_snapshot_startpoint(
        base_tag=BASE_IMAGE_TAG,
        snapshot_tag=STARTPOINT_IMAGE_TAG,
    )

    return STARTPOINT_IMAGE_TAG


@pytest.fixture
def ephemeral_container(
    container_engine: str,
    e2e_container_snapshot: str,
) -> Generator[EphemeralContainer, None, None]:
    """Function fixture: Spawns a clean ephemeral container from the snapshot for a test.

    Step 4: Execute test in the container, report result, then revert container to clean by destroying instance.
    """
    tw_model = os.environ.get("PI_E2E_TW_MODEL", os.environ.get("PI_E2E_MODEL", DEFAULT_TW_MODEL))
    imp_model = os.environ.get("PI_E2E_IMP_MODEL", os.environ.get("PI_E2E_MODEL", DEFAULT_IMP_MODEL))
    assessor = os.environ.get("PI_E2E_ASSESSOR", DEFAULT_ASSESSOR_MODEL)
    provider = os.environ.get("HARNESS_PI_PROVIDER", "llama-swap")

    manager = ContainerLifecycleManager(engine=container_engine)
    container = manager.spawn_ephemeral_container(
        snapshot_tag=e2e_container_snapshot,
        tw_model=tw_model,
        imp_model=imp_model,
        assessor=assessor,
        provider=provider,
    )

    try:
        yield container
    finally:
        keep = os.environ.get("KEEP_CONTAINER_ON_FAIL", "0") == "1"
        if not keep:
            container.destroy()
        else:
            print(f"\n[E2E] Retaining container instance: {container.container_name}")
