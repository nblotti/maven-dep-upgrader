"""Preflight environment checks.

Verifies required external tools are present, the Nexus endpoint is reachable,
and (for mutating runs) the git working tree is clean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import proc
from .config import Config
from .git_status import user_dirty_lines


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", fatal: bool = True) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, fatal=fatal))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.fatal)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.fatal]

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "OK " if c.ok else ("XX " if c.fatal else "-- ")
            line = f"[{mark}] {c.name}"
            if c.detail:
                line += f": {c.detail}"
            lines.append(line)
        return "\n".join(lines)


def _git_tree_clean(repo: Path) -> tuple[bool, str]:
    res = proc.run(["git", "status", "--porcelain"], cwd=repo)
    if not res.ok:
        return False, "git status failed (is this a git repo?)"
    user_dirty = user_dirty_lines(res.stdout)
    if user_dirty:
        paths = [porcelain_path(l) for l in user_dirty if porcelain_path(l)]
        preview = ", ".join(paths[:5])
        more = f" (+{len(paths) - 5} more)" if len(paths) > 5 else ""
        return False, f"working tree not clean ({len(user_dirty)} changes: {preview}{more})"
    if res.stdout.strip():
        return True, "clean (tool workdir only)"
    return True, "clean"


def _nexus_reachable(cfg: Config) -> tuple[bool, str]:
    if not cfg.nexus.configured:
        return False, "nexus.base_url / repositories not configured"
    try:
        import requests

        url = cfg.nexus.base_url.rstrip("/") + "/service/rest/v1/status"
        auth = None
        if cfg.nexus_user and cfg.nexus_password:
            auth = (cfg.nexus_user, cfg.nexus_password)
        resp = requests.get(url, auth=auth, timeout=10)
        if resp.status_code < 500:
            return True, f"reachable (HTTP {resp.status_code})"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:  # network/DNS/etc.
        return False, f"unreachable: {type(exc).__name__}"


def run_preflight(
    cfg: Config,
    *,
    need_mutation: bool = False,
    need_codex: bool = False,
    need_mr: bool = False,
    check_nexus: bool = True,
) -> PreflightReport:
    """Run preflight checks appropriate to the requested operation."""
    report = PreflightReport()
    repo = cfg.repo

    report.add("repo path exists", repo.is_dir(), str(repo))
    report.add("git present", proc.which("git") is not None, fatal=True)
    report.add(
        "maven present",
        proc.which(cfg.maven.mvn_executable) is not None,
        cfg.maven.mvn_executable,
        # Discovery can fall back to raw POMs for planning; mutation needs mvn.
        fatal=need_mutation,
    )

    if cfg.maven.settings:
        report.add(
            "maven settings.xml exists",
            Path(cfg.maven.settings).is_file(),
            cfg.maven.settings,
            fatal=False,
        )

    if check_nexus:
        ok, detail = _nexus_reachable(cfg)
        report.add("nexus reachable", ok, detail, fatal=False)

    if need_codex:
        report.add(
            "codex present",
            proc.which(cfg.codex.executable) is not None,
            cfg.codex.executable,
        )
        key_ok = bool(cfg.codex_api_key)
        # Codex fix is required for fix-codex / ask / skip-failing (compile path).
        need_key = (
            cfg.codex.require_api_key
            and need_mutation
            and cfg.run.baseline in ("fix-codex", "ask", "skip-failing")
        )
        env_label = ", ".join(cfg.codex.api_key_envs)
        key_detail = cfg.codex_api_key_env or "env"
        if not cfg.codex.require_api_key:
            report.add(
                "codex API key check",
                True,
                "skipped (codex.require_api_key=false)",
                fatal=False,
            )
        else:
            report.add(
                f"API key ({env_label}) set",
                key_ok,
                key_detail,
                fatal=need_key,
            )

    if need_mr:
        report.add("glab present", proc.which("glab") is not None, fatal=False)
        report.add(
            "GITLAB_TOKEN set or glab auth",
            bool(cfg.gitlab_token),
            "env (glab auth login also works)",
            fatal=False,
        )
        report.add(
            "gitlab.project configured",
            cfg.gitlab.configured,
            cfg.gitlab.project or "<unset>",
        )

    if need_mutation and repo.is_dir():
        ok, detail = _git_tree_clean(repo)
        report.add("git tree clean", ok, detail, fatal=True)

    return report
