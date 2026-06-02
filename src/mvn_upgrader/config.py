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


@dataclass
class RunConfig:
    on_failure: str = "skip"  # skip | abort
    report_dir: str = "."

    def __post_init__(self) -> None:
        if self.on_failure not in ("skip", "abort"):
            raise ConfigError(
                f"run.on_failure must be 'skip' or 'abort', got {self.on_failure!r}"
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
    def openai_api_key(self) -> Optional[str]:
        return os.environ.get("OPENAI_API_KEY")

    def secret_values(self) -> list[str]:
        """All secret-shaped values to redact from captured logs."""
        vals = [
            self.nexus_password,
            self.nexus_user,
            self.gitlab_token,
            self.openai_api_key,
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
