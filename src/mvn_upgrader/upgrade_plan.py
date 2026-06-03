"""Editable CSV upgrade plan with explicit execution order.

``plan --export`` writes ``upgrade-plan.csv`` with an ``order`` column (1, 2, 3…).
Edit the file before running:

- ``order=0`` — skip this row (no upgrade).
- Same ``order`` on multiple rows — applied in one round (one build / Codex cycle).
- Rows sorted by ``order`` ascending; ties keep file order within the round.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .models import Kind, PlanItem, VersionSource

log = logging.getLogger("mvn_upgrader.upgrade_plan")

PLAN_CSV_NAME = "upgrade-plan.csv"

_COLUMNS = (
    "order",
    "kind",
    "group_id",
    "artifact_id",
    "current_version",
    "target_version",
    "version_source",
    "co_moved",
    "reason",
)


class PlanFileError(ValueError):
    pass


@dataclass(frozen=True)
class PlanRow:
    order: int
    kind: Kind
    group_id: str
    artifact_id: str
    current_version: str
    target_version: str
    version_source: VersionSource
    co_moved: tuple[str, ...]
    reason: str

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind.value, self.ga)


def plan_csv_path(cfg: Config, path: Optional[str] = None) -> Path:
    if path:
        p = Path(path)
        return p if p.is_absolute() else cfg.repo / p
    return cfg.repo / cfg.run.report_dir / PLAN_CSV_NAME


def export_plan_csv(
    cfg: Config,
    plan: list[PlanItem],
    *,
    path: Optional[str] = None,
) -> Path:
    """Write planned upgrades to CSV with default sequential order 1..N."""
    out = plan_csv_path(cfg, path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        w.writeheader()
        for idx, item in enumerate(plan, start=1):
            a = item.artifact
            w.writerow({
                "order": idx,
                "kind": a.kind.value,
                "group_id": a.group_id,
                "artifact_id": a.artifact_id,
                "current_version": a.current_version or "",
                "target_version": item.target_version,
                "version_source": a.version_source.value,
                "co_moved": ";".join(item.co_moved),
                "reason": item.reason,
            })
    return out


def _parse_row(raw: dict[str, str], line_no: int) -> PlanRow:
    try:
        order = int((raw.get("order") or "0").strip())
    except ValueError as exc:
        raise PlanFileError(f"line {line_no}: invalid order {raw.get('order')!r}") from exc
    kind_s = (raw.get("kind") or "").strip()
    try:
        kind = Kind(kind_s)
    except ValueError as exc:
        raise PlanFileError(f"line {line_no}: invalid kind {kind_s!r}") from exc
    gid = (raw.get("group_id") or "").strip()
    aid = (raw.get("artifact_id") or "").strip()
    if not gid or not aid:
        raise PlanFileError(f"line {line_no}: group_id and artifact_id required")
    vs_s = (raw.get("version_source") or "literal").strip()
    try:
        vs = VersionSource(vs_s)
    except ValueError as exc:
        raise PlanFileError(f"line {line_no}: invalid version_source {vs_s!r}") from exc
    co = (raw.get("co_moved") or "").strip()
    co_moved = tuple(x.strip() for x in co.split(";") if x.strip())
    return PlanRow(
        order=order,
        kind=kind,
        group_id=gid,
        artifact_id=aid,
        current_version=(raw.get("current_version") or "").strip(),
        target_version=(raw.get("target_version") or "").strip(),
        version_source=vs,
        co_moved=co_moved,
        reason=(raw.get("reason") or "").strip(),
    )


def load_plan_csv(path: Path) -> list[PlanRow]:
    if not path.is_file():
        raise PlanFileError(f"plan file not found: {path}")
    rows: list[PlanRow] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise PlanFileError("plan file is empty")
        missing = set(_COLUMNS) - set(reader.fieldnames)
        if missing:
            raise PlanFileError(f"plan file missing columns: {sorted(missing)}")
        for line_no, raw in enumerate(reader, start=2):
            if not any(v and str(v).strip() for v in raw.values()):
                continue
            rows.append(_parse_row(raw, line_no))
    return rows


def resolve_plan_from_file(
    plan: list[PlanItem],
    rows: list[PlanRow],
) -> list[list[PlanItem]]:
    """Match CSV rows to discovered plan items; return batches grouped by order.

    Rows with ``order=0`` are skipped. Unknown GAs log a warning and are ignored.
    """
    by_key: dict[tuple[str, str], PlanItem] = {
        (i.artifact.kind.value, i.ga): i for i in plan
    }
    # Preserve file order within the same order number.
    active = [(i, r) for i, r in enumerate(rows) if r.order > 0]
    active.sort(key=lambda t: (t[1].order, t[0]))

    batches: list[list[PlanItem]] = []
    current_order: Optional[int] = None
    current_batch: list[PlanItem] = []

    for _, row in active:
        if not row.target_version:
            raise PlanFileError(f"{row.ga}: target_version is empty")
        base = by_key.get(row.key)
        if base is None:
            log.warning("plan file row %s not in auto-generated plan; skipping", row.ga)
            continue
        item = PlanItem(
            artifact=base.artifact,
            target_version=row.target_version,
            co_moved=list(row.co_moved) if row.co_moved else list(base.co_moved),
            reason=row.reason or base.reason,
        )
        if current_order is None:
            current_order = row.order
        if row.order != current_order:
            if current_batch:
                batches.append(current_batch)
            current_batch = []
            current_order = row.order
        current_batch.append(item)

    if current_batch:
        batches.append(current_batch)
    return batches
