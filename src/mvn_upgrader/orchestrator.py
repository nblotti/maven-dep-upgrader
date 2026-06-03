"""Main orchestration loop.

Ties discovery -> Nexus/policy -> plan -> (apply -> build -> codex -> commit) ->
report -> MR together. Built up across milestones; commands degrade gracefully
when later stages are not wired in yet.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import Config
from .preflight import run_preflight

log = logging.getLogger("mvn_upgrader.orchestrator")


def cmd_plan(cfg: Config, *, export_path: Optional[str] = None) -> int:
    pf = run_preflight(cfg, need_mutation=False)
    print(pf.render())
    if not pf.ok:
        print("\npreflight failed; cannot build plan.")
        return 1
    from .planner import build_and_report_plan

    plan, results = build_and_report_plan(cfg, export_path=export_path)
    _print_plan_summary(plan, results)
    return 0


def cmd_run(
    cfg: Config,
    *,
    apply: bool = False,
    create_mr: bool = False,
    only: Optional[list[str]] = None,
    max_items: Optional[int] = None,
    plan_file: Optional[str] = None,
) -> int:
    if not apply:
        print("(no --apply: running in plan-only mode)\n")
        return cmd_plan(cfg)

    from .runner import run_upgrades

    return run_upgrades(
        cfg,
        create_mr=create_mr,
        only=only or [],
        max_items=max_items,
        plan_file=plan_file,
    )


def cmd_report(cfg: Config) -> int:
    from .report import regenerate_from_state

    path = regenerate_from_state(cfg)
    if path is None:
        print("no prior run state found; nothing to regenerate.")
        return 1
    print(f"regenerated report from state -> {path}")
    return 0


def _print_plan_summary(plan, results) -> None:
    print(f"\nPlan: {len(plan)} upgrade(s).")
    for item in plan:
        a = item.artifact
        extra = f"  (+{len(item.co_moved)} co-moved)" if item.co_moved else ""
        print(
            f"  {a.kind.value:10} {a.ga}  {a.current_version} -> "
            f"{item.target_version}  [{a.version_source.value}]{extra}"
        )
    info = [r for r in results if r.status.value == "informational"]
    if info:
        print(f"\nInformational (no editable version): {len(info)}")
