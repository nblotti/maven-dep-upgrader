"""Configuration loading & validation.

Config values come from a YAML file (see ``config.example.yaml``); secrets come
*only* from environment variables and are never read from the YAML or written to
logs/reports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(ValueError):
    """Raised when the config file is missing required values or malformed."""


def _expand(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    return os.path.expanduser(os.path.expandvars(path))


@dataclass
class MavenConfig:
    settings: Optional[str] = None
    build_command: str = "mvn -B -ntp clean verify"
    mvn_executable: str = "mvn"
    # Surefire/failsafe test selectors to exclude from builds. Normally set at
    # runtime (pre-existing failing tests discovered by the baseline check), but
    # may be seeded from config too.
    test_excludes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.settings = _expand(self.settings)


@dataclass
class NexusConfig:
    base_url: str = ""
    repositories: list[str] = field(default_factory=list)
    extension: str = "jar"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.repositories)


@dataclass
class PolicyConfig:
    allow_major: bool = False
    exclude_prerelease: bool = True
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    ignore_versions: list[str] = field(default_factory=list)
    pin: dict[str, str] = field(default_factory=dict)
    # If true, refuse property bumps that would move >1 dependency.
    one_at_a_time: bool = False


@dataclass
class GitConfig:
    base_branch: str = "main"
    branch_prefix: str = "chore/dependency-upgrades"
    remote: str = "origin"


@dataclass
class GitlabConfig:
    project: str = ""
    host: Optional[str] = None  # for self-managed; falls back to GITLAB_HOST env

    @property
    def configured(self) -> bool:
        return bool(self.project)


@dataclass
class CodexConfig:
    executable: str = "codex"
    sandbox: str = "workspace-write"
    max_fix_attempts: int = 4
    bypass_sandbox: bool = False  # use --dangerously-bypass-approvals-and-sandbox
    # When True, stop early if a Codex attempt leaves the build with the exact
    # same error (likely stuck). When False, keep trying the full
    # ``max_fix_attempts`` even if the error is unchanged.
    stop_on_no_progress: bool = True
    # Env var name(s) checked for an API key (first non-empty wins). Override when
    # Codex uses LiteLLM or another backend with a non-OpenAI env var name.
    api_key_envs: list[str] = field(default_factory=lambda: ["OPENAI_API_KEY"])
    # Set true only if you want preflight to abort when none of ``api_key_envs`` is set.
    # Codex reads credentials from the shell env and/or ~/.codex/ config; the tool never
    # calls the LLM API directly.
    require_api_key: bool = False
    # Extra env vars merged into the Codex subprocess (values support $VAR expansion).
    extra_env: dict[str, str] = field(default_factory=dict)


_BASELINE_CHOICES = ("ask", "abort", "fix-codex", "skip-failing", "off")


@dataclass
class RunConfig:
    on_failure: str = "skip"  # skip | abort
    report_dir: str = "."
    # Pre-upgrade baseline build handling:
    #   ask          - prompt: Codex fix / skip failing tests / abort
    #   fix-codex    - use Codex to fix any pre-existing build failure, then upgrade
    #   skip-failing - exclude pre-existing failing tests; compile errors → fix-codex
    #   abort        - stop if baseline is red (fix manually)
    #   off          - do not run a baseline build
    baseline: str = "ask"
    # Follow-along run log (relative to repo_path). Default: .mvn-upgrade-work/run.log
    log_file: Optional[str] = None

    def __post_init__(self) -> None:
        if self.on_failure not in ("skip", "abort"):
            raise ConfigError(
                f"run.on_failure must be 'skip' or 'abort', got {self.on_failure!r}"
            )
        if self.baseline not in _BASELINE_CHOICES:
            # Legacy: ``fix`` used to mean "abort for manual fix".
            if self.baseline == "fix":
                self.baseline = "abort"
            else:
                raise ConfigError(
                    f"run.baseline must be one of {_BASELINE_CHOICES}, "
                    f"got {self.baseline!r}"
                )


@dataclass
class Config:
    repo_path: str = "."
    maven: MavenConfig = field(default_factory=MavenConfig)
    nexus: NexusConfig = field(default_factory=NexusConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    git: GitConfig = field(default_factory=GitConfig)
    gitlab: GitlabConfig = field(default_factory=GitlabConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    run: RunConfig = field(default_factory=RunConfig)

    def __post_init__(self) -> None:
        self.repo_path = _expand(self.repo_path) or "."

    # ---- secrets (env-only) -------------------------------------------------
    @property
    def nexus_user(self) -> Optional[str]:
        return os.environ.get("NEXUS_USER")

    @property
    def nexus_password(self) -> Optional[str]:
        return os.environ.get("NEXUS_PASSWORD")

    @property
    def gitlab_token(self) -> Optional[str]:
        return os.environ.get("GITLAB_TOKEN")

    @property
    def gitlab_host(self) -> Optional[str]:
        return self.gitlab.host or os.environ.get("GITLAB_HOST")

    @property
    def codex_api_key(self) -> Optional[str]:
        for name in self.codex.api_key_envs:
            value = os.environ.get(name)
            if value:
                return value
        return None

    @property
    def codex_api_key_env(self) -> Optional[str]:
        """Name of the first env var that supplied ``codex_api_key``."""
        for name in self.codex.api_key_envs:
            if os.environ.get(name):
                return name
        return None

    @property
    def openai_api_key(self) -> Optional[str]:
        """Backward-compatible alias; prefer ``codex_api_key``."""
        return self.codex_api_key

    def secret_values(self) -> list[str]:
        """All secret-shaped values to redact from captured logs."""
        vals = [
            self.nexus_password,
            self.nexus_user,
            self.gitlab_token,
            self.codex_api_key,
        ]
        return [v for v in vals if v]

    @property
    def repo(self) -> Path:
        return Path(self.repo_path).resolve()

    def with_overrides(self, **kwargs: Any) -> "Config":
        """Return a shallow copy with top-level fields overridden (for CLI flags)."""
        return replace(self, **kwargs)


_SECTION_TYPES = {
    "maven": MavenConfig,
    "nexus": NexusConfig,
    "policy": PolicyConfig,
    "git": GitConfig,
    "gitlab": GitlabConfig,
    "codex": CodexConfig,
    "run": RunConfig,
}


def _build_section(name: str, raw: Any):
    cls = _SECTION_TYPES[name]
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"config section '{name}' must be a mapping")
    known = cls.__dataclass_fields__.keys()
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown keys in '{name}': {sorted(unknown)}")
    return cls(**raw)


def from_dict(data: dict[str, Any]) -> Config:
    data = dict(data or {})
    sections = {}
    for name in _SECTION_TYPES:
        sections[name] = _build_section(name, data.pop(name, None))
    repo_path = data.pop("repo_path", ".")
    unknown = set(data)
    if unknown:
        raise ConfigError(f"unknown top-level config keys: {sorted(unknown)}")
    return Config(repo_path=repo_path, **sections)


def load_config(path: Optional[str]) -> Config:
    """Load config from a YAML file path, or return defaults if path is None."""
    if path is None:
        return Config()
    p = Path(_expand(path))
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError("top-level config must be a mapping")
    return from_dict(data)
