"""Git status helpers — ignore tool-managed paths when checking a clean tree."""

from __future__ import annotations

from .build import WORKDIR_NAME


def porcelain_path(line: str) -> str:
    """Extract the file path from a ``git status --porcelain`` line."""
    if len(line) < 4:
        return ""
    return line[3:].strip().split(" -> ")[-1]


def is_ignored_tool_path(path: str) -> bool:
    """True for files/dirs created by mvn-upgrade itself (not user changes)."""
    norm = path.strip().replace("\\", "/")
    if not norm:
        return False
    if norm == WORKDIR_NAME or norm.startswith(WORKDIR_NAME + "/"):
        return True
    return False


def user_dirty_lines(porcelain_output: str) -> list[str]:
    """Return porcelain lines that represent real user changes (not tool artifacts)."""
    dirty: list[str] = []
    for line in porcelain_output.splitlines():
        if not line.strip():
            continue
        path = porcelain_path(line)
        if path and not is_ignored_tool_path(path):
            dirty.append(line)
    return dirty


def is_clean_excluding_tool_artifacts(porcelain_output: str) -> bool:
    return len(user_dirty_lines(porcelain_output)) == 0
