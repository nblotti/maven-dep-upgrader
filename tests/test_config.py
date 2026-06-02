import os

import pytest

from mvn_upgrader.config import Config, ConfigError, from_dict, load_config


def test_defaults():
    cfg = Config()
    assert cfg.policy.allow_major is False
    assert cfg.policy.exclude_prerelease is True
    assert cfg.run.on_failure == "skip"
    assert cfg.codex.max_fix_attempts == 4
    assert cfg.git.base_branch == "main"


def test_from_dict_full():
    data = {
        "repo_path": ".",
        "nexus": {"base_url": "https://n.example", "repositories": ["maven-public"]},
        "policy": {"allow_major": True, "pin": {"g:a": "1.2.3"}},
        "gitlab": {"project": "grp/repo"},
        "run": {"on_failure": "abort"},
    }
    cfg = from_dict(data)
    assert cfg.nexus.configured is True
    assert cfg.policy.allow_major is True
    assert cfg.policy.pin == {"g:a": "1.2.3"}
    assert cfg.gitlab.configured is True
    assert cfg.run.on_failure == "abort"


def test_unknown_top_level_key():
    with pytest.raises(ConfigError):
        from_dict({"nope": 1})


def test_unknown_section_key():
    with pytest.raises(ConfigError):
        from_dict({"policy": {"bogus": 1}})


def test_bad_on_failure():
    with pytest.raises(ConfigError):
        from_dict({"run": {"on_failure": "explode"}})


def test_baseline_default_and_valid():
    assert Config().run.baseline == "ask"
    assert from_dict({"run": {"baseline": "skip-failing"}}).run.baseline == "skip-failing"
    assert from_dict({"run": {"baseline": "fix-codex"}}).run.baseline == "fix-codex"


def test_baseline_legacy_fix_alias_maps_to_abort():
    assert from_dict({"run": {"baseline": "fix"}}).run.baseline == "abort"


def test_bad_baseline():
    with pytest.raises(ConfigError):
        from_dict({"run": {"baseline": "nope"}})


def test_maven_test_excludes_default_empty():
    assert Config().maven.test_excludes == []


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_PASSWORD", "hunter2")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-xxx")
    cfg = Config()
    assert cfg.nexus_password == "hunter2"
    assert cfg.gitlab_token == "glpat-xxx"
    assert "hunter2" in cfg.secret_values()


def test_load_config_missing_file():
    with pytest.raises(ConfigError):
        load_config("/nonexistent/whatever.yaml")


def test_load_config_none_returns_defaults():
    assert isinstance(load_config(None), Config)


def test_example_config_loads():
    here = os.path.dirname(os.path.dirname(__file__))
    cfg = load_config(os.path.join(here, "config.example.yaml"))
    assert cfg.nexus.base_url.startswith("https://")
    assert cfg.gitlab.project
