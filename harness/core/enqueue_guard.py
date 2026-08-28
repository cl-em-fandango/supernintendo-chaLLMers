"""Enqueue guard: which pending files may become tasks.

A plan parent (an epic) is a requirement archive, not work. Its card file
carries a `DO NOT EXECUTE THIS FILE AS A CARD.` directive naming the leaf
tickets that own its acceptance criteria, and
`plan-2026-08-26-done/SLICING-MAP.md` is explicit: parent files "must not be
enqueued when marked **DO NOT EXECUTE**". Enqueuing one spends a session
re-deriving work its leaves already own, so the check runs at the single
boundary where a pending file becomes a task —
`providers.DirectoryTaskProvider.fetch_pending()`.

Detection is deliberately narrow so it cannot veto an ordinary task: the
phrase must be uppercase and sit inside a blockquote directive within the
first `HEADER_WINDOW` lines, which is where the plan convention puts it
(line 3 of every parent card). Prose that merely says "do not execute" in a
body paragraph enqueues normally.

The refusal carries the leaf ids named by the directive, in the order the
directive lists them, so the operator sees what to enqueue instead — the map's
enqueue rule wants a rejection to name the leaf sequence, and the directive is
the copy of that sequence inside the file itself. No plan file is parsed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# The directive phrase, exactly as the plan writes it. Uppercase on purpose:
# a lowercase mention in prose is not a refusal to enqueue.
MARKER = re.compile(r"\bDO NOT EXECUTE\b")

# A ticket id (`T04`, `T72`) inside the directive blockquote.
TICKET_ID = re.compile(r"\bT\d+\b")

# A task id at the start of a file stem (`T04-merge-abort` -> `T04`).
STEM_ID = re.compile(r"^T\d+")

# How many leading lines are scanned for the directive. Parent cards put it on
# line 3; anything further down is body text, not a header directive.
HEADER_WINDOW = 10


@dataclass(frozen=True)
class EnqueueDecision:
    """The guard's answer for one pending file.

    `leaves` are the ticket ids the directive points at (empty when the
    directive names none); `directive` is the marker text with its blockquote
    and emphasis stripped, so a log line quotes the rule that fired rather than
    markdown. Both are empty on an allowed file.
    """
    allowed: bool
    filename: str = ""
    leaves: tuple[str, ...] = ()
    directive: str = ""

    @property
    def reason(self) -> str:
        """Why the file was refused; "" when it is allowed.

        Worded for a log line: it names the file, the marker, and what to
        enqueue in its place.
        """
        if self.allowed:
            return ""
        target = ", ".join(self.leaves) if self.leaves else "the leaves it lists"
        return (f"{self.filename or 'task file'} is a plan parent marked "
                f"DO NOT EXECUTE — enqueue {target} instead")


def check_enqueue(body: str, filename: str = "") -> EnqueueDecision:
    """May a task file with this body be enqueued? See the module docstring."""
    directive = _directive(body)
    if not directive:
        return EnqueueDecision(allowed=True, filename=filename)
    return EnqueueDecision(allowed=False, filename=filename,
                           leaves=_leaves(directive, _own_id(filename)),
                           directive=directive)


def _directive(body: str) -> str:
    """The blockquote text carrying the marker, markdown stripped; "" if none.

    Only `HEADER_WINDOW` leading lines are read, and only blockquote lines are
    considered, so the marker has to appear where the plan convention places a
    parent directive rather than anywhere in the file.
    """
    quoted = [line.strip() for line in body.splitlines()[:HEADER_WINDOW]
              if line.lstrip().startswith(">")]
    if not any(MARKER.search(line) for line in quoted):
        return ""
    return re.sub(r"\s+", " ", " ".join(line.lstrip("> ") for line in quoted)
                  ).replace("**", "").strip()


def _leaves(directive: str, own_id: str) -> tuple[str, ...]:
    """Ticket ids the directive names, first-seen order, minus the parent itself."""
    seen: list[str] = []
    for ticket in TICKET_ID.findall(directive):
        if ticket != own_id and ticket not in seen:
            seen.append(ticket)
    return tuple(seen)


def _own_id(filename: str) -> str:
    """The parent's own ticket id from its file stem, so it is never listed
    as one of its own leaves. "" when the stem does not start with one."""
    match = STEM_ID.match(Path(filename).stem) if filename else None
    return match.group(0) if match else ""
