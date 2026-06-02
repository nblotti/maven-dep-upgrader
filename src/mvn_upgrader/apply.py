"""Apply a single version bump using one of four strategies.

Primary path: ``versions-maven-plugin`` goals (battle-tested). Fallback: a
precise, targeted XML edit of the declaring POM, used for plugins (no goal can
pin an exact plugin version) and whenever Maven is unavailable.

Exactly one artifact is changed per call so each becomes its own commit.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import proc
from .config import Config
from .models import Kind, PlanItem, VersionSource

log = logging.getLogger("mvn_upgrader.apply")


@dataclass
class ApplyResult:
    ok: bool
    strategy: str
    via: str  # "mvn" | "text-edit"
    detail: str = ""


def _mvn_base(cfg: Config) -> list[str]:
    cmd = [cfg.maven.mvn_executable, "-B", "-ntp"]
    if cfg.maven.settings:
        cmd += ["-s", cfg.maven.settings]
    return cmd


def build_mvn_command(cfg: Config, item: PlanItem) -> Optional[list[str]]:
    """Build the versions-maven-plugin command for an item, or None if no goal fits."""
    a = item.artifact
    t = item.target_version
    base = _mvn_base(cfg)

    if a.version_source == VersionSource.PROPERTY and a.property_name:
        return base + [
            "versions:set-property",
            f"-Dproperty={a.property_name}",
            f"-DnewVersion={t}",
            "-DgenerateBackupPoms=false",
        ]
    if a.version_source == VersionSource.PARENT:
        return base + [
            "versions:update-parent",
            f"-DparentVersion=[{t}]",
            "-DgenerateBackupPoms=false",
        ]
    if a.kind == Kind.PLUGIN:
        # No versions goal pins an exact plugin version -> use targeted edit.
        return None
    if a.version_source in (VersionSource.LITERAL, VersionSource.MANAGED):
        return base + [
            "versions:use-dep-version",
            f"-Dincludes={a.ga}",
            f"-DdepVersion={t}",
            "-DforceVersion=true",
            "-DgenerateBackupPoms=false",
        ]
    return None


# --------------------------------------------------------------------------- #
# Targeted text edits (fallback / plugin path)
# --------------------------------------------------------------------------- #
def edit_property(text: str, prop: str, new: str) -> tuple[str, int]:
    pat = re.compile(rf"(<{re.escape(prop)}>)\s*[^<]*?\s*(</{re.escape(prop)}>)")
    return pat.subn(rf"\g<1>{new}\g<2>", text, count=1)


_ELEMENT_RE = re.compile(r"<(dependency|plugin)\b[^>]*>.*?</\1>", re.DOTALL)
_GID_RE = re.compile(r"<groupId>\s*([^<]+?)\s*</groupId>")
_AID_RE = re.compile(r"<artifactId>\s*([^<]+?)\s*</artifactId>")
_VER_RE = re.compile(r"<version>\s*([^<]*?)\s*</version>")


def edit_artifact_version(
    text: str, group_id: str, artifact_id: str, new: str
) -> tuple[str, int]:
    """Replace the literal ``<version>`` of a matching dependency/plugin block.

    Property-driven versions (``${...}``) are left untouched. Returns the new
    text and the number of blocks edited.
    """
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        block = m.group(0)
        gid = _GID_RE.search(block)
        aid = _AID_RE.search(block)
        if not aid or aid.group(1) != artifact_id:
            return block
        if gid and group_id and gid.group(1) != group_id:
            return block
        ver = _VER_RE.search(block)
        if not ver or ver.group(1).startswith("${"):
            return block
        count += 1
        return block[: ver.start()] + f"<version>{new}</version>" + block[ver.end():]

    new_text = _ELEMENT_RE.sub(repl, text)
    return new_text, count


_PARENT_RE = re.compile(r"<parent\b[^>]*>.*?</parent>", re.DOTALL)


def edit_parent_version(
    text: str, group_id: str, artifact_id: str, new: str
) -> tuple[str, int]:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        block = m.group(0)
        aid = _AID_RE.search(block)
        if not aid or aid.group(1) != artifact_id:
            return block
        ver = _VER_RE.search(block)
        if not ver:
            return block
        count += 1
        return block[: ver.start()] + f"<version>{new}</version>" + block[ver.end():]

    return _PARENT_RE.sub(repl, text), count


def apply_via_text_edit(item: PlanItem) -> ApplyResult:
    a = item.artifact
    if not a.declared_in:
        return ApplyResult(False, a.version_source.value, "text-edit",
                           "no declaring POM to edit (managed externally)")
    path = Path(a.declared_in)
    if not path.is_file():
        return ApplyResult(False, a.version_source.value, "text-edit",
                           f"declaring POM not found: {path}")
    text = path.read_text(encoding="utf-8")

    if a.version_source == VersionSource.PROPERTY and a.property_name:
        new_text, n = edit_property(text, a.property_name, item.target_version)
        strat = "property"
    elif a.version_source == VersionSource.PARENT:
        new_text, n = edit_parent_version(
            text, a.group_id, a.artifact_id, item.target_version
        )
        strat = "parent"
    else:
        new_text, n = edit_artifact_version(
            text, a.group_id, a.artifact_id, item.target_version
        )
        strat = a.kind.value if a.kind == Kind.PLUGIN else a.version_source.value

    if n == 0:
        return ApplyResult(False, strat, "text-edit",
                           "no matching version element found to edit")
    path.write_text(new_text, encoding="utf-8")
    return ApplyResult(True, strat, "text-edit", f"edited {n} element(s) in {path.name}")


def apply_item(cfg: Config, item: PlanItem, runner=proc.run) -> ApplyResult:
    """Apply a single bump; prefer the mvn goal, fall back to a targeted edit."""
    cmd = build_mvn_command(cfg, item)
    if cmd is not None and proc.which(cfg.maven.mvn_executable):
        res = runner(cmd, cwd=cfg.repo)
        if res.ok:
            return ApplyResult(True, item.artifact.version_source.value, "mvn",
                               "applied via versions-maven-plugin")
        log.warning(
            "mvn apply failed for %s (rc=%s); falling back to text edit",
            item.ga, res.returncode,
        )
    return apply_via_text_edit(item)
