"""Thin, safe subprocess wrapper.

All external commands go through here. We never use ``shell=True`` with
interpolated strings; commands are always passed as argument lists.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger("mvn_upgrader.proc")


@dataclass
class ProcResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        if self.stderr:
            return self.stdout + ("\n" if self.stdout else "") + self.stderr
        return self.stdout


def which(program: str) -> Optional[str]:
    """Return the resolved path of an executable, or None if not on PATH."""
    return shutil.which(program)


def run(
    args: Sequence[str],
    *,
    cwd: Optional[str | Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    check: bool = False,
    redact: Optional[Sequence[str]] = None,
) -> ProcResult:
    """Run a command and capture stdout/stderr as text.

    ``redact`` is a list of secret strings stripped from a *log* of the command
    line (not from the returned output, which the caller redacts before writing).
    """
    arglist = [str(a) for a in args]
    log.debug("exec: %s", _redacted(" ".join(arglist), redact))
    proc = subprocess.run(
        arglist,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = ProcResult(
        args=arglist,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if check and not result.ok:
        raise subprocess.CalledProcessError(
            result.returncode, arglist, output=result.stdout, stderr=result.stderr
        )
    return result


def _redacted(text: str, secrets: Optional[Sequence[str]]) -> str:
    if not secrets:
        return text
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
