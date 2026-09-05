"""Typed configuration for the `snes-demo` web-app deployment feature (FR-9).

The `demo` section of `config.json` is parsed once into a `DemoConfig`
parameters object; every later demo module reads this object instead of
constants or raw dict keys. A config file without a `demo` section still
loads and yields every default, with the feature switched off
(`enabled=False`) so existing deployments keep working untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Feature master switch. When false, `snes-demo` labels are ignored by
# inbound sync and the pipeline demo hooks are no-ops.
DEFAULT_DEMO_ENABLED = False

# Long-lived branch the demo apps are published to (Pages source branch).
DEFAULT_DEPLOY_BRANCH = "pi/app-demo"

# Repo-root source directory for generated apps.
DEFAULT_APPS_DIR = "demo-apps"

# Pages artifact directory on the deploy branch only.
DEFAULT_DOCS_DIR = "docs"

# Model used for FR-4 site-content generation.
DEFAULT_CONTENT_MODEL = "GLM4.5-AIR_Q4_K_M"

# Subject of the nonsense/fallback content (FR-4.2).
DEFAULT_FALLBACK_TOPIC = "History of Morris Dancing"

# Directory name under the work dir for the dedicated deploy checkout when
# `demo.deployDir` is not configured.
DEFAULT_DEPLOY_DIR_NAME = "demo-deploy"


@dataclass(frozen=True)
class DemoConfig:
    """The FR-9 knobs as one explicit parameters object for the demo feature."""

    enabled: bool
    deploy_branch: str
    apps_dir: str
    docs_dir: str
    content_model: str
    fallback_topic: str
    deploy_dir: Path


def parse_demo_config(raw: dict[str, Any], work_dir: Path) -> DemoConfig:
    """Build a `DemoConfig` from the `demo` section of a raw config dict.

    A missing or empty section yields all defaults; `deployDir` defaults to
    `<harnessExecutionAndQueueDir>/demo-deploy` so the deploy checkout never lands in the
    harness workdir's task areas.
    """
    section = raw.get("demo") or {}
    deploy_dir_raw = section.get("deployDir")
    deploy_dir = (
        Path(deploy_dir_raw).expanduser()
        if deploy_dir_raw
        else work_dir / DEFAULT_DEPLOY_DIR_NAME
    )
    return DemoConfig(
        enabled=bool(section.get("enabled", DEFAULT_DEMO_ENABLED)),
        deploy_branch=str(section.get("deployBranch", DEFAULT_DEPLOY_BRANCH)),
        apps_dir=str(section.get("appsDir", DEFAULT_APPS_DIR)),
        docs_dir=str(section.get("docsDir", DEFAULT_DOCS_DIR)),
        content_model=str(section.get("contentModel", DEFAULT_CONTENT_MODEL)),
        fallback_topic=str(section.get("fallbackTopic", DEFAULT_FALLBACK_TOPIC)),
        deploy_dir=deploy_dir,
    )
