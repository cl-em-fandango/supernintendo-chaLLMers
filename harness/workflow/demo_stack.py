"""Stack selection and Pages base-path derivation for generated demo apps.

Demo spec FR-3.2: the tooling a ticket explicitly requests wins; an
unspecified ticket gets the default stack — create-react-app with Material
UI and a dark theme. This module turns that rule (and the FR-7.5 rule that
static assets must work under a GitHub Pages project-site subpath) into
one explicit `StackPlan` the scaffolder and the build runner read, so no
later module hardcodes a stack name, a build command or a public path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

class WebStack(Enum):
    """The discrete set of stacks a demo app may be generated with."""

    CRA_MUI = "cra-mui"        # create-react-app + Material UI, dark theme
    VUE = "vue"
    PLAIN_HTML = "plain-html"  # static index.html, no build step


# Detection order: the most specific request phrasings first.
_STACK_REQUESTS: tuple[tuple[WebStack, tuple[re.Pattern, ...]], ...] = (
    (WebStack.PLAIN_HTML, (
        re.compile(r"\bplain\s+html\b"),
        re.compile(r"\bstatic\s+html\b"),
        re.compile(r"\bvanilla\s+html\b"),
        re.compile(r"\bhtml\s+only\b"),
        re.compile(r"\bno\s+(?:build|framework)\b"),
    )),
    (WebStack.VUE, (
        re.compile(r"\bvue(?:\.?js)?\b"),
    )),
    (WebStack.CRA_MUI, (
        re.compile(r"\bcreate[-_ ]?react[-_ ]?app\b"),
        re.compile(r"\bcra\b"),
        re.compile(r"\breact\b"),
        re.compile(r"\bmaterial[\s-]?ui\b|\bmui\b"),
    )),
)

# FR-3.2: the default when the ticket is not specific about tooling.
DEFAULT_STACK = WebStack.CRA_MUI


@dataclass(frozen=True)
class StackPlan:
    """How one demo app is built and served, decided once at generation.

    `build_commands` are argv tuples (program first) executed in the app
    directory through the npm boundary; `artifact_dir` is the stack's
    standard build-output directory, relative to the app directory;
    `public_path` is the GitHub Pages project-site subpath the assets
    must be built for (FR-7.5).
    """

    stack: WebStack
    build_commands: tuple[tuple[str, ...], ...]
    artifact_dir: str
    public_path: str
    needs_build: bool


def repo_name(repo: str) -> str:
    """The bare repository name from an `owner/repo` slug or URL.

    A Pages project site is served under `/<repo>/`, so the name — not
    the slug — is what the base/public path is built from.
    """
    text = str(repo or "").strip().removesuffix(".git")
    return text.rsplit("/", 1)[-1] or "app"


def pages_base_path(repo: str) -> str:
    """The Pages project-site subpath a build must use (FR-7.5)."""
    return f"/{repo_name(repo)}/"


def detect_stack(ticket_text: str) -> WebStack:
    """The stack the ticket explicitly requests, else the default (FR-3.2)."""
    text = str(ticket_text or "").lower()
    for stack, patterns in _STACK_REQUESTS:
        if any(pattern.search(text) for pattern in patterns):
            return stack
    return DEFAULT_STACK


def build_stack_plan(stack: WebStack, repo: str) -> StackPlan:
    """The build/public-path plan for one stack (FR-3.2, FR-7.5)."""
    public_path = pages_base_path(repo)
    if stack is WebStack.PLAIN_HTML:
        return StackPlan(
            stack=stack,
            build_commands=(),
            artifact_dir=".",
            public_path=public_path,
            needs_build=False)
    if stack is WebStack.VUE:
        return StackPlan(
            stack=stack,
            build_commands=(("install",), ("run", "build")),
            artifact_dir="dist",
            public_path=public_path,
            needs_build=True)
    return StackPlan(
        stack=WebStack.CRA_MUI,
        build_commands=(("install",), ("run", "build")),
        artifact_dir="build",
        public_path=public_path,
        needs_build=True)
