"""Report generation + run-state persistence.

Writes both a human-readable ``dependency-updates.md`` (also used as the MR
description) and a machine-readable ``dependency-updates.json`` (run state used
for auditing and resume).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Config
from .models import Status, UpgradeResult

log = logging.getLogger("mvn_upgrader.report")

MD_NAME = "dependency-updates.md"
JSON_NAME = "dependency-updates.json"

_STATUS_ORDER = [
    Status.UPGRADED,
    Status.PENDING,
    Status.SKIPPED_BUILD_FAILED,
    Status.SKIPPED_NO_NEWER,
    Status.NOT_IN_NEXUS,
    Status.MANAGED_EXTERNAL,
    Status.INFORMATIONAL,
    Status.ERROR,
]


@dataclass
class RunState:
    generated_at: str = ""
    mode: str = "plan"  # plan | run
    branch: Optional[str] = None
    base_branch: Optional[str] = None
    mr_url: Optional[str] = None
    used_fallback: bool = False
    results: list[UpgradeResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "mode": self.mode,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "mr_url": self.mr_url,
            "used_fallback": self.used_fallback,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        return cls(
            generated_at=d.get("generated_at", ""),
            mode=d.get("mode", "plan"),
            branch=d.get("branch"),
            base_branch=d.get("base_branch"),
            mr_url=d.get("mr_url"),
            used_fallback=d.get("used_fallback", False),
            results=[UpgradeResult.from_dict(r) for r in d.get("results", [])],
        )

    def by_status(self, status: Status) -> list[UpgradeResult]:
        return [r for r in self.results if r.status == status]

    def result_for(self, ga: str, kind: Optional[str] = None) -> Optional[UpgradeResult]:
        for r in self.results:
            if r.ga == ga and (kind is None or r.kind.value == kind):
                return r
        return None


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(state: RunState) -> str:
    lines: list[str] = []
    lines.append(f"# Dependency updates {state.generated_at}")
    lines.append("")

    counts: dict[str, int] = {}
    for r in state.results:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1

    lines.append("## Summary")
    lines.append("")
    if state.branch:
        lines.append(f"- Branch: `{state.branch}` (base: `{state.base_branch}`)")
    if state.mr_url:
        lines.append(f"- Merge request: {state.mr_url}")
    if state.used_fallback:
        lines.append(
            "- Note: effective versions derived from raw POMs "
            "(Maven effective-pom unavailable)."
        )
    for status in _STATUS_ORDER:
        n = counts.get(status.value, 0)
        if n:
            lines.append(f"- {status.value}: {n}")
    lines.append("")

    lines.append("## Artifacts")
    lines.append("")
    header = (
        "| Artifact | Kind | Old | New | Source | Status | Commit | "
        "Fix attempts | Notes |"
    )
    lines.append(header)
    lines.append("|" + "---|" * 9)
    actionable = [
        r for r in state.results
        if r.status not in (Status.INFORMATIONAL,)
    ]
    for r in _sorted_results(actionable):
        commit = (r.commit or "")[:10]
        notes = "; ".join(r.notes)
        if r.co_moved:
            extra = "moves: " + ", ".join(r.co_moved)
            notes = f"{notes}; {extra}" if notes else extra
        lines.append(
            "| {ga} | {kind} | {old} | {new} | {src} | {status} | {commit} | "
            "{fix} | {notes} |".format(
                ga=_md_escape(r.ga),
                kind=r.kind.value,
                old=_md_escape(r.old_version or ""),
                new=_md_escape(r.new_version or ""),
                src=r.version_source.value,
                status=r.status.value,
                commit=commit,
                fix=r.fix_attempts,
                notes=_md_escape(notes),
            )
        )
    lines.append("")

    failed = [
        r for r in state.results
        if r.status in (Status.SKIPPED_BUILD_FAILED, Status.ERROR)
    ]
    if failed:
        lines.append("## Skipped / failed")
        lines.append("")
        for r in failed:
            note = "; ".join(r.notes) or "(no detail)"
            lines.append(f"- `{r.ga}` ({r.status.value}): {_md_escape(note)}")
        lines.append("")

    info = state.by_status(Status.INFORMATIONAL)
    if info:
        lines.append("## Informational (no editable version)")
        lines.append("")
        for r in _sorted_results(info):
            note = "; ".join(r.notes)
            lines.append(f"- `{r.ga}` ({r.old_version or '?'}): {_md_escape(note)}")
        lines.append("")

    return "\n".join(lines)


def _sorted_results(results: list[UpgradeResult]) -> list[UpgradeResult]:
    return sorted(results, key=lambda r: (r.kind.value, r.ga))


def report_paths(cfg: Config) -> tuple[Path, Path]:
    base = Path(cfg.run.report_dir)
    if not base.is_absolute():
        base = cfg.repo / base
    return base / MD_NAME, base / JSON_NAME


def write_reports(cfg: Config, state: RunState) -> tuple[Path, Path]:
    md_path, json_path = report_paths(cfg)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(state), encoding="utf-8")
    json_path.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=False), encoding="utf-8"
    )
    return md_path, json_path


def load_state(cfg: Config) -> Optional[RunState]:
    _, json_path = report_paths(cfg)
    if not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return RunState.from_dict(data)


def regenerate_from_state(cfg: Config) -> Optional[Path]:
    state = load_state(cfg)
    if state is None:
        return None
    md_path, _ = report_paths(cfg)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(state), encoding="utf-8")
    return md_path
