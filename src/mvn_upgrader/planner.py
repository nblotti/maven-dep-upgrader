"""Build an UpgradePlan from discovered artifacts + Nexus + policy.

This is read-only: it never mutates the repo. It produces the list of
``PlanItem`` to act on plus ``UpgradeResult`` records (PENDING for planned
upgrades, plus not-in-nexus / no-newer / managed-external / informational) that
feed the report.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import versioning
from .config import Config
from .models import (
    Artifact,
    Kind,
    PlanItem,
    Status,
    UpgradeResult,
    VersionSource,
)
from .nexus import NexusClient
from .pom import discover
from .report import RunState, now_iso, write_reports

log = logging.getLogger("mvn_upgrader.planner")


def _result(
    artifact: Artifact,
    status: Status,
    *,
    new_version: Optional[str] = None,
    co_moved: Optional[list[str]] = None,
    notes: Optional[list[str]] = None,
) -> UpgradeResult:
    return UpgradeResult(
        group_id=artifact.group_id,
        artifact_id=artifact.artifact_id,
        kind=artifact.kind,
        version_source=artifact.version_source,
        old_version=artifact.current_version,
        new_version=new_version,
        status=status,
        co_moved=co_moved or [],
        notes=notes or [],
    )


def _pin_for(cfg: Config, ga: str) -> Optional[str]:
    return cfg.policy.pin.get(ga)


def _select(cfg: Config, current: str, versions: list[str], ga: str) -> Optional[str]:
    return versioning.select_target(
        current,
        versions,
        allow_major=cfg.policy.allow_major,
        exclude_prerelease=cfg.policy.exclude_prerelease,
        ignore_versions=cfg.policy.ignore_versions,
        pin=_pin_for(cfg, ga),
    )


def build_plan(
    cfg: Config,
    artifacts: list[Artifact],
    nexus: Optional[NexusClient],
) -> tuple[list[PlanItem], list[UpgradeResult]]:
    plan: list[PlanItem] = []
    results: list[UpgradeResult] = []

    inc, exc = cfg.policy.include, cfg.policy.exclude
    considered: list[Artifact] = []
    for a in artifacts:
        if not versioning.ga_allowed(a.ga, inc, exc):
            continue
        considered.append(a)

    # Partition out the items that can't be planned without Nexus lookups.
    upgradeable: list[Artifact] = []
    for a in considered:
        if a.version_source == VersionSource.MANAGED and a.declared_in is None:
            results.append(
                _result(
                    a,
                    Status.MANAGED_EXTERNAL,
                    notes=["version managed by an external parent/BOM; "
                           "upgrade the parent/BOM coordinate instead"],
                )
            )
            continue
        if not a.current_version:
            results.append(
                _result(a, Status.INFORMATIONAL,
                        notes=["no resolvable/editable version"])
            )
            continue
        upgradeable.append(a)

    if nexus is None:
        for a in upgradeable:
            results.append(
                _result(a, Status.INFORMATIONAL,
                        notes=["Nexus not configured; cannot check versions"])
            )
        return plan, results

    # Group property-driven artifacts so a single property bump is one plan item.
    prop_groups: dict[tuple[str, str], list[Artifact]] = {}
    singles: list[Artifact] = []
    for a in upgradeable:
        if a.version_source == VersionSource.PROPERTY and a.property_name:
            key = (a.property_name, a.declared_in or "")
            prop_groups.setdefault(key, []).append(a)
        else:
            singles.append(a)

    _plan_singles(cfg, nexus, singles, plan, results)
    _plan_property_groups(cfg, nexus, prop_groups, plan, results)

    plan.sort(key=lambda i: (i.artifact.kind.value, i.artifact.ga))
    return plan, results


def _versions(nexus: NexusClient, a: Artifact) -> list[str]:
    return [c.version for c in nexus.list_versions(a.group_id, a.artifact_id)]


def _plan_singles(cfg, nexus, singles, plan, results):
    for a in singles:
        versions = _versions(nexus, a)
        if not versions:
            results.append(
                _result(a, Status.NOT_IN_NEXUS,
                        notes=["GA not found in configured Nexus repositories"])
            )
            continue
        target = _select(cfg, a.current_version, versions, a.ga)
        if target is None:
            results.append(_result(a, Status.SKIPPED_NO_NEWER))
            continue
        plan.append(PlanItem(artifact=a, target_version=target))
        results.append(
            _result(a, Status.PENDING, new_version=target)
        )


def _plan_property_groups(cfg, nexus, prop_groups, plan, results):
    for (prop, _decl), members in prop_groups.items():
        members = sorted(members, key=lambda a: a.ga)
        current = next((m.current_version for m in members if m.current_version), None)

        if cfg.policy.one_at_a_time and len(members) > 1:
            for m in members:
                results.append(
                    _result(m, Status.INFORMATIONAL, notes=[
                        f"shared property ${{{prop}}} drives "
                        f"{len(members)} deps; skipped (one_at_a_time)"
                    ])
                )
            continue

        # Each member must exist in Nexus; the chosen version must be available
        # for ALL members so the single property bump is safe for everyone.
        member_versions: dict[str, set[str]] = {}
        missing = False
        for m in members:
            vs = _versions(nexus, m)
            if not vs:
                results.append(
                    _result(m, Status.NOT_IN_NEXUS, notes=[
                        f"GA not in Nexus; cannot safely bump shared "
                        f"property ${{{prop}}}"])
                )
                missing = True
            member_versions[m.ga] = set(vs)
        if missing or not current:
            for m in members:
                if m.ga in member_versions and member_versions[m.ga]:
                    results.append(_result(m, Status.SKIPPED_NO_NEWER, notes=[
                        f"shared property ${{{prop}}} could not be bumped "
                        "(a co-member is not upgradeable)"]))
            continue

        common = set.intersection(*member_versions.values()) if member_versions else set()
        target = _select(cfg, current, sorted(common), members[0].ga)
        if target is None:
            for m in members:
                results.append(_result(m, Status.SKIPPED_NO_NEWER, notes=[
                    f"no common newer version across deps sharing "
                    f"${{{prop}}}"]))
            continue

        primary = members[0]
        co_moved = [m.ga for m in members[1:]]
        plan.append(
            PlanItem(artifact=primary, target_version=target, co_moved=co_moved,
                     reason=f"shared property ${{{prop}}}")
        )
        results.append(
            _result(primary, Status.PENDING, new_version=target, co_moved=co_moved,
                    notes=[f"property ${{{prop}}}"])
        )


def build_and_report_plan(
    cfg: Config,
    *,
    export_path: Optional[str] = None,
) -> tuple[list[PlanItem], list[UpgradeResult]]:
    disc = discover(cfg)
    nexus = NexusClient.from_config(cfg) if cfg.nexus.configured else None
    if nexus is None:
        log.warning("Nexus is not configured; versions cannot be checked.")
    plan, results = build_plan(cfg, disc.artifacts, nexus)

    state = RunState(
        generated_at=now_iso(),
        mode="plan",
        base_branch=cfg.git.base_branch,
        used_fallback=disc.used_fallback,
        results=results,
    )
    md_path, json_path = write_reports(cfg, state)
    log.info("wrote %s and %s", md_path, json_path)
    print(f"\nReport written to:\n  {md_path}\n  {json_path}")

    if plan:
        from .upgrade_plan import export_plan_csv

        csv_path = export_plan_csv(cfg, plan, path=export_path)
        print(f"  {csv_path}")
        print("  Edit the 'order' column (0=skip, same number=same round), then:")
        print(f"  mvn-upgrade run --config ... --apply --plan-file {csv_path.name}")

    return plan, results
