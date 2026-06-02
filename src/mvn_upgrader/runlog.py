"""Follow-along run log (``tail -f`` friendly).

Duplicates stdout/stderr to a line-buffered file under the repo workdir so you
can watch progress from another terminal while a long upgrade run is in flight.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from .build import WORKDIR_NAME
from .config import Config

_active_path: Optional[Path] = None
_log_handle: Optional[TextIO] = None
_orig_stdout: Optional[TextIO] = None
_orig_stderr: Optional[TextIO] = None
_log_handler: Optional[logging.Handler] = None


class _Tee(TextIO):
    """Write to the original stream and the run log; flush after every write."""

    def __init__(self, stream: TextIO, log_file: TextIO) -> None:
        self._stream = stream
        self._log = log_file

    def write(self, data: str) -> int:
        self._stream.write(data)
        self._log.write(data)
        self._stream.flush()
        self._log.flush()
        return len(data)

    def flush(self) -> None:
        self._stream.flush()
        self._log.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()


def run_log_path(cfg: Config) -> Path:
    rel = cfg.run.log_file or f"{WORKDIR_NAME}/run.log"
    p = Path(rel)
    if not p.is_absolute():
        p = cfg.repo / p
    return p


def is_active() -> bool:
    return _log_handle is not None


def activate(cfg: Config) -> Path:
    """Start teeing stdout/stderr to the run log. Returns the log file path."""
    global _active_path, _log_handle, _orig_stdout, _orig_stderr, _log_handler

    if is_active():
        return _active_path  # type: ignore[return-value]

    path = run_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w", encoding="utf-8", buffering=1)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fh.write(f"=== mvn-upgrade run log started {started} ===\n")
    fh.write(f"repo: {cfg.repo}\n\n")
    fh.flush()

    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    _log_handle = fh
    _active_path = path
    sys.stdout = _Tee(_orig_stdout, fh)  # type: ignore[assignment]
    sys.stderr = _Tee(_orig_stderr, fh)  # type: ignore[assignment]

    root = logging.getLogger()
    _log_handler = logging.StreamHandler(fh)
    _log_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _log_handler.setLevel(logging.DEBUG)
    root.addHandler(_log_handler)

    return path


def deactivate() -> None:
    """Restore stdout/stderr and close the run log."""
    global _active_path, _log_handle, _orig_stdout, _orig_stderr, _log_handler

    if not is_active():
        return

    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
        _log_handler = None

    if _orig_stdout is not None:
        sys.stdout = _orig_stdout
    if _orig_stderr is not None:
        sys.stderr = _orig_stderr

    if _log_handle is not None:
        _log_handle.write("\n=== mvn-upgrade run log ended ===\n")
        _log_handle.flush()
        _log_handle.close()

    _log_handle = None
    _active_path = None
    _orig_stdout = None
    _orig_stderr = None
