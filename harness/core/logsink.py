"""The single log sink: echo a line to stdout and append it to one file.

One implementation of "where a log line goes" for the whole harness. The
composition root and the CLI handlers used to each carry their own print-only
`_log`, so nothing ever reached `work/logs/harness.log`.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

LOG_ENCODING = "utf-8"
DEFAULT_MAX_BYTES = 5_000_000
ROTATION_SUFFIX = ".1"


class LogSink:
    """Write every log record to stdout and to `path`, rotating at `max_bytes`.

    The file record is formatted and UTF-8 encoded *before* its byte length is
    compared with `max_bytes`, so what we measure is exactly what we write.
    Rotation keeps one generation: `<path>` is moved aside as `<path>.1`,
    replacing the previous generation (same rule as `supervisor.log`).

    A call never raises: on any `OSError` the sink degrades to echo-only and
    says so once per process, because a wedged, full or missing disk must not
    break a pipeline run.
    """

    def __init__(self, path: Path | None, echo: bool = True,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = path
        self.echo = echo
        self.max_bytes = max_bytes
        self._handle = None
        self._warned = False

    def __call__(self, line: str = "") -> None:
        """Echo `line` and append it, timestamped, to the log file."""
        record = f"[{self._timestamp()}] {line}\n".encode(LOG_ENCODING)
        if self.echo:
            try:
                print(line, flush=True)
            except OSError as exc:
                self._warn(exc)
        self._append(record)

    def close(self) -> None:
        """Close the underlying file. Mainly for tests."""
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:
                self._warn(exc)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _timestamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def _append(self, record: bytes) -> None:
        if self.path is None:
            return
        try:
            if self._current_size() + len(record) > self.max_bytes:
                self._rotate()
            handle = self._open()
            handle.write(record)
            handle.flush()
        except OSError as exc:
            self._warn(exc)

    def _open(self):
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("ab")
        return self._handle

    def _current_size(self) -> int:
        """Size of the current log in bytes; 0 when it does not exist yet."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _rotate(self) -> None:
        """Move the log aside as `<path>.1`, replacing the previous generation.

        The handle is dropped first so the renamed inode is no longer written
        to, and reopened lazily at the original path afterwards. If the rename
        fails we keep appending un-rotated to the same file.
        """
        self.close()
        try:
            os.replace(self.path,
                       self.path.with_name(self.path.name + ROTATION_SUFFIX))
        except OSError as exc:
            self._warn(exc)

    def _warn(self, exc: OSError) -> None:
        """Report a sink failure once per process, never raising."""
        if self._warned:
            return
        self._warned = True
        target = self.path if self.path is not None else "<no log path>"
        print(f"harness: WARNING log write to {target} failed ({exc}); "
              "continuing without the log file (warning shown once)",
              file=sys.stderr, flush=True)
