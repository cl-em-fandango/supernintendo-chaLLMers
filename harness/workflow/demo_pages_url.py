"""The GitHub Pages project-site URL derived from an `owner/repo` slug.

Demo spec FR-8.2: both demo success comments carry
`https://<owner>.github.io/<repo>/`, derived from the linkage sidecar's
repo. The harness never probes the URL (edge case 6) — this module only
formats it.
"""
from __future__ import annotations


def pages_url(repo: str) -> str:
    """The Pages project-site URL for an `owner/repo` slug.

    A slug without a `/` degrades to the bare-name shape rather than
    raising: the comment is observability, and a malformed repo string
    must not cost the operator the deployment signal.
    """
    owner, slash, name = str(repo).strip().partition("/")
    if not slash:
        owner, name = "", str(repo).strip()
    return f"https://{owner}.github.io/{name}/"
