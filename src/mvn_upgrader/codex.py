"""Invoke the OpenAI Codex CLI (``codex exec``) to fix a red build.

The orchestrator owns the loop and always re-runs Maven to judge success; we
never trust Codex's self-report. Codex is asked to fix *only* the breakage
caused by the current dependency bump.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from . import proc
from .config import Config
from .models import PlanItem
from .redact import redact

log = logging.getLogger("mvn_upgrader.codex")


@dataclass
class CodexResult:
    invoked: bool
    exit_code: int
    stdout: str
    stderr: str


PROMPT_TEMPLATE = (
    "The build is failing after upgrading `{ga}` from `{old}` to `{new}` in this "
    "Maven project.\n"
    "Fix only the compilation/test breakages caused by this dependency change.\n"
    "Hard rules: do NOT change the version of `{ga}` or any other dependency; "
    "do NOT touch unrelated modules; do NOT edit tests to make them pass unless "
    "the test itself calls a changed API; keep changes minimal. "
    "The build command is `{build_cmd}`.\n"
    "Build error output:\n"
    "```\n{log_tail}\n```\n"
)


AGENTS_MD = """# Build instructions for automated fixes

This repository is being upgraded one dependency at a time by an automated tool.

- Build / test command: `{build_cmd}`
- A green build means `{build_cmd}` exits 0.

When asked to fix a failing build:
- ONLY fix breakages caused by the dependency version bump described in the prompt.
- Do NOT change any dependency or plugin versions.
- Do NOT modify unrelated modules.
- Do NOT weaken or delete tests to make them pass unless the test calls a changed API.
- Some tests may have been ALREADY failing before the upgrade and are excluded
  from the build; do not touch them. Never disable, skip, or delete a NEWLY
  failing test to make the build pass -- fix the production code instead.
- Keep changes minimal and focused.
"""


def agents_md_content(cfg: Config) -> str:
    return AGENTS_MD.format(build_cmd=cfg.maven.build_command)


_PREEXISTING_NOTE = (
    "\nThese tests were ALREADY failing before this upgrade and are excluded "
    "from the build; they are NOT your concern, do not touch them:\n"
    "{listing}\n"
    "Fix ONLY the code/tests newly broken by this dependency change, and fix "
    "them by changing production code. Do NOT disable, skip, @Disabled/@Ignore, "
    "delete, or otherwise weaken any newly failing test to make the build pass.\n"
)


BASELINE_PROMPT_TEMPLATE = (
    "The build is failing BEFORE any dependency upgrades have been applied in "
    "this Maven project.\n"
    "Your job is to make the build GREEN. The build command is `{build_cmd}`.\n"
    "Work iteratively until it passes:\n"
    "1. Apply a fix.\n"
    "2. Run `{build_cmd}` yourself to verify.\n"
    "3. If it still fails, read the new error and fix again.\n"
    "Repeat until `{build_cmd}` exits 0. Do NOT stop or report success until you "
    "have actually run the build and seen it pass — verify, don't assume.\n"
    "Rules:\n"
    "- You MAY add missing dependencies or plugins to pom.xml when code "
    "references libraries that are not declared (e.g. missing joda-time).\n"
    "- Do NOT change versions of dependencies that are already declared — "
    "version bumps are handled separately by another step.\n"
    "- Fix tests by correcting production or test code and configuration, NOT "
    "by @Disabled/@Ignore, deleting tests, or skipping unless unavoidable.\n"
    "- Keep changes minimal and focused on making the build green.\n"
    "Initial build error output:\n"
    "```\n{log_tail}\n```\n"
)


def build_baseline_prompt(build_cmd: str, log_tail: str) -> str:
    return BASELINE_PROMPT_TEMPLATE.format(build_cmd=build_cmd, log_tail=log_tail)


def build_prompt(
    item: PlanItem,
    build_cmd: str,
    log_tail: str,
    preexisting_failures: Optional[list[str]] = None,
) -> str:
    a = item.artifact
    prompt = PROMPT_TEMPLATE.format(
        ga=a.ga,
        old=a.current_version or "?",
        new=item.target_version,
        build_cmd=build_cmd,
        log_tail=log_tail,
    )
    if preexisting_failures:
        listing = "\n".join(f"- {s}" for s in preexisting_failures)
        prompt += _PREEXISTING_NOTE.format(listing=listing)
    return prompt


def build_codex_command(cfg: Config, prompt: str) -> list[str]:
    # Custom CLI / wrapper: caller fully specifies the args via a template.
    if cfg.codex.args:
        repo = str(cfg.repo)
        return [cfg.codex.executable] + [
            tok.replace("{repo}", repo).replace("{prompt}", prompt)
            for tok in cfg.codex.args
        ]
    cmd = [cfg.codex.executable, "exec", "--cd", str(cfg.repo)]
    if cfg.codex.bypass_sandbox:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        # `codex exec` is non-interactive and never prompts for approval, so it
        # rejects --ask-for-approval; only the sandbox policy is relevant here.
        cmd += ["--sandbox", cfg.codex.sandbox]
    cmd += [prompt]
    return cmd


def codex_env(cfg: Config) -> dict[str, str]:
    """Environment passed to the Codex subprocess (inherits shell + ``extra_env``)."""
    env = dict(os.environ)
    for key, value in cfg.codex.extra_env.items():
        env[key] = os.path.expandvars(value)
    return env


def run_fix(
    cfg: Config,
    item: PlanItem,
    log_tail: str,
    *,
    build_cmd: str,
    runner=proc.run,
) -> CodexResult:
    """Ask Codex to fix the breakage. Returns its exit code/output (not trusted)."""
    preexisting = getattr(cfg.maven, "test_excludes", None) or None
    prompt = build_prompt(item, build_cmd, log_tail, preexisting_failures=preexisting)
    cmd = build_codex_command(cfg, prompt)

    res = runner(cmd, cwd=cfg.repo, env=codex_env(cfg), redact=cfg.secret_values())
    return CodexResult(
        invoked=True,
        exit_code=res.returncode,
        stdout=redact(res.stdout, cfg.secret_values()),
        stderr=redact(res.stderr, cfg.secret_values()),
    )


def run_baseline_fix(
    cfg: Config,
    log_tail: str,
    *,
    build_cmd: str,
    runner=proc.run,
) -> CodexResult:
    """Ask Codex to fix pre-existing build failures (before any upgrade)."""
    prompt = build_baseline_prompt(build_cmd, log_tail)
    cmd = build_codex_command(cfg, prompt)
    res = runner(cmd, cwd=cfg.repo, env=codex_env(cfg), redact=cfg.secret_values())
    return CodexResult(
        invoked=True,
        exit_code=res.returncode,
        stdout=redact(res.stdout, cfg.secret_values()),
        stderr=redact(res.stderr, cfg.secret_values()),
    )
