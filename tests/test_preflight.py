import subprocess

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.preflight import run_preflight


def _clean_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "pom.xml").write_text("<project/>")
    subprocess.run(["git", "add", "pom.xml"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)


def test_preflight_codex_key_custom_env(monkeypatch, tmp_path):
    _clean_repo(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("MY_LITELLM_KEY", "secret-key")
    cfg = Config(repo_path=str(tmp_path))
    cfg.codex.api_key_envs = ["MY_LITELLM_KEY", "OPENAI_API_KEY"]
    cfg.codex.require_api_key = True
    cfg.run.baseline = "fix-codex"

    report = run_preflight(cfg, need_mutation=False, need_codex=True, check_nexus=False)
    rendered = report.render()
    assert "API key (MY_LITELLM_KEY, OPENAI_API_KEY) set" in rendered
    assert "[OK ] API key" in rendered
    assert "MY_LITELLM_KEY" in rendered


def test_preflight_codex_key_skip_when_not_required(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config()
    cfg.codex.require_api_key = False
    cfg.run.baseline = "fix-codex"

    report = run_preflight(cfg, need_mutation=False, need_codex=True, check_nexus=False)
    rendered = report.render()
    assert "codex API key check" in rendered
    assert "skipped (codex.require_api_key=false)" in rendered


def test_preflight_codex_key_fatal_for_fix_codex(monkeypatch, tmp_path):
    _clean_repo(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = Config(repo_path=str(tmp_path))
    cfg.codex.require_api_key = True
    cfg.run.baseline = "fix-codex"

    report = run_preflight(cfg, need_mutation=True, need_codex=True, check_nexus=False)
    assert not report.ok
    assert any("API key" in c.name for c in report.failures)
