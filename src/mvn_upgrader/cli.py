"""Command-line interface for maven-dep-upgrader.

    mvn-upgrade plan   [--config c.yaml]
    mvn-upgrade run    [--config c.yaml] [--apply] [--create-mr]
                       [--only g:a,...] [--max N] [--on-failure skip|abort]
    mvn-upgrade report [--config c.yaml]

Without ``--apply``, ``run`` behaves like ``plan`` (no mutations).
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from .config import ConfigError, load_config


def _split_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvn-upgrade",
        description="Automated Maven dependency & plugin upgrader.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", help="path to config YAML", default=None)
        p.add_argument(
            "--repo",
            help="override repo_path from config",
            default=None,
        )

    p_plan = sub.add_parser("plan", help="discover + report only (no mutations)")
    add_common(p_plan)
    p_plan.add_argument(
        "--export",
        nargs="?",
        const="upgrade-plan.csv",
        metavar="PATH",
        help="write editable upgrade-plan.csv (default: upgrade-plan.csv in report_dir)",
    )

    p_run = sub.add_parser("run", help="perform upgrades (requires --apply to mutate)")
    add_common(p_run)
    p_run.add_argument(
        "--apply",
        action="store_true",
        help="actually mutate the repo (without it, run behaves like plan)",
    )
    p_run.add_argument(
        "--create-mr",
        action="store_true",
        help="push the branch and open a GitLab MR if anything was upgraded",
    )
    p_run.add_argument(
        "--only",
        help="comma-separated g:a list to restrict the run to",
        default=None,
    )
    p_run.add_argument("--max", type=int, default=None, help="cap number of upgrades")
    p_run.add_argument(
        "--on-failure",
        choices=["skip", "abort"],
        default=None,
        help="override run.on_failure",
    )
    p_run.add_argument(
        "--baseline",
        choices=["ask", "abort", "fix-codex", "skip-failing", "off"],
        default=None,
        help="pre-upgrade baseline handling: ask (prompt), fix-codex (Codex fixes "
             "red build before upgrades), skip-failing (exclude failing tests), "
             "abort (stop if red), off",
    )
    p_run.add_argument(
        "--plan-file",
        nargs="?",
        const="upgrade-plan.csv",
        metavar="PATH",
        help="run upgrades from editable CSV (from plan); order=0 skips, same order=batch",
    )

    p_report = sub.add_parser("report", help="regenerate report from last run state")
    add_common(p_report)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else (
            logging.INFO if args.verbose == 1 else logging.WARNING
        ),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Imported lazily so `--help` works without heavy deps loaded.
    from . import orchestrator
    from . import runlog

    try:
        cfg = load_config(args.config)
        if getattr(args, "repo", None):
            cfg = cfg.with_overrides(repo_path=args.repo)

        use_runlog = args.command in ("plan", "run")
        if use_runlog:
            log_path = runlog.activate(cfg)
            print(f"Run log: {log_path}")
            print(f"  follow in another terminal: tail -f {log_path}\n")

        try:
            if args.command == "plan":
                export = getattr(args, "export", None)
                return orchestrator.cmd_plan(cfg, export_path=export)
            if args.command == "report":
                return orchestrator.cmd_report(cfg)
            if args.command == "run":
                on_failure = args.on_failure or cfg.run.on_failure
                baseline = args.baseline or cfg.run.baseline
                cfg = cfg.with_overrides(run=cfg.run.__class__(
                    on_failure=on_failure, report_dir=cfg.run.report_dir,
                    baseline=baseline,
                    log_file=cfg.run.log_file,
                ))
                return orchestrator.cmd_run(
                    cfg,
                    apply=args.apply,
                    create_mr=args.create_mr,
                    only=_split_csv(args.only),
                    max_items=args.max,
                    plan_file=getattr(args, "plan_file", None),
                )
        finally:
            if use_runlog:
                runlog.deactivate()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130

    parser.error("unknown command")
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
