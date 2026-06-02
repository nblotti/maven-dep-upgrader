"""Maven build runner + error-signature computation.

Runs the configured build, captures combined stdout/stderr to a log file (with
secrets redacted), keeps the tail for Codex, and computes a stable
``error_signature`` so the orchestrator can detect when Codex makes no progress.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import proc
from .config import Config
from .redact import redact

log = logging.getLogger("mvn_upgrader.build")

WORKDIR_NAME = ".mvn-upgrade-work"
_TAIL_LINES = 400

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,\d]*")
_UNIX_PATH = re.compile(r"(?:/[^\s:()\[\]]+)+")
_WIN_PATH = re.compile(r"[A-Za-z]:\\[^\s:()\[\]]+")
_LINECOL = re.compile(r":\d+(?::\d+)?")
_NUM = re.compile(r"\b\d+\b")

_ERROR_HINT = re.compile(r"(\[ERROR\]|ERROR\]|BUILD FAILURE|Caused by:|Tests run:)")


@dataclass
class BuildResult:
    ok: bool
    exit_code: int
    log_path: Optional[str]
    tail: str
    error_signature: Optional[str]


def normalize_error_lines(output: str) -> list[str]:
    """Extract & normalize error-relevant lines (paths/timestamps/numbers stripped)."""
    selected: list[str] = []
    for raw in output.splitlines():
        line = _ANSI.sub("", raw)
        if not _ERROR_HINT.search(line):
            continue
        line = _TS.sub("<ts>", line)
        line = _UNIX_PATH.sub("<path>", line)
        line = _WIN_PATH.sub("<path>", line)
        line = _LINECOL.sub(":<n>", line)
        line = _NUM.sub("<n>", line)
        line = line.strip()
        # Drop the generic Maven failure banners that add no discriminating info.
        if line in ("[ERROR] BUILD FAILURE", "BUILD FAILURE"):
            continue
        if line:
            selected.append(line)
    return selected


def error_signature(output: str) -> Optional[str]:
    lines = normalize_error_lines(output)
    if not lines:
        return None
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest[:16]


def build_command(cfg: Config) -> list[str]:
    parts = shlex.split(cfg.maven.build_command)
    if not parts:
        parts = [cfg.maven.mvn_executable, "-B", "clean", "verify"]
    # Ensure the Nexus-mirror settings.xml is used by the build too.
    if cfg.maven.settings and "-s" not in parts and "--settings" not in parts:
        parts = [parts[0], "-s", cfg.maven.settings, *parts[1:]]
    parts += _test_exclude_args(cfg, parts)
    return parts


def _test_exclude_args(cfg: Config, parts: list[str]) -> list[str]:
    """Build ``-Dtest=!...`` args to skip pre-existing failing tests.

    Skipped when the user's build_command already pins ``-Dtest`` (we must not
    clobber an explicit selection).
    """
    excludes = getattr(cfg.maven, "test_excludes", None) or []
    if not excludes:
        return []
    if any(p == "-Dtest" or p.startswith("-Dtest=") for p in parts):
        log.warning(
            "build_command already sets -Dtest; not injecting baseline exclusions"
        )
        return []
    negated = ",".join(f"!{sel}" for sel in excludes)
    return [
        f"-Dtest={negated}",
        f"-Dit.test={negated}",
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "-Dfailsafe.failIfNoSpecifiedTests=false",
    ]


def workdir(cfg: Config) -> Path:
    d = cfg.repo / WORKDIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(cfg: Config, *, attempt_tag: str = "build", runner=proc.run) -> BuildResult:
    cmd = build_command(cfg)
    res = runner(cmd, cwd=cfg.repo)
    combined = redact(res.combined, cfg.secret_values())

    wd = workdir(cfg)
    log_path = wd / f"{attempt_tag}.log"
    try:
        log_path.write_text(combined, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem edge
        log.warning("could not write build log: %s", exc)
        log_path = None  # type: ignore[assignment]

    tail = "\n".join(combined.splitlines()[-_TAIL_LINES:])
    return BuildResult(
        ok=res.ok,
        exit_code=res.returncode,
        log_path=str(log_path) if log_path else None,
        tail=tail,
        error_signature=None if res.ok else error_signature(combined),
    )
