import pytest

from mvn_upgrader.codex import (
    build_codex_command,
    build_prompt,
    run_fix,
)
from mvn_upgrader.config import Config
from mvn_upgrader.models import Artifact, Kind, PlanItem, VersionSource
from mvn_upgrader.proc import ProcResult


def _item():
    art = Artifact(
        group_id="com.google.guava", artifact_id="guava",
        current_version="32.0.0-jre", kind=Kind.DEPENDENCY,
        version_source=VersionSource.PROPERTY, declared_in="pom.xml",
        property_name="guava.version",
    )
    return PlanItem(artifact=art, target_version="32.1.0-jre")


def test_prompt_contains_rules_and_versions():
    p = build_prompt(_item(), "mvn -B clean verify", "boom\nstack")
    assert "com.google.guava:guava" in p
    assert "32.0.0-jre" in p and "32.1.0-jre" in p
    assert "do NOT change the version" in p
    assert "boom" in p


def test_prompt_without_preexisting_has_no_note():
    p = build_prompt(_item(), "mvn verify", "log")
    assert "ALREADY failing" not in p


def test_prompt_with_preexisting_failures_note():
    p = build_prompt(_item(), "mvn verify", "log",
                     preexisting_failures=["FooTest#bar", "BazTest"])
    assert "ALREADY failing" in p
    assert "FooTest#bar" in p and "BazTest" in p
    assert "Do NOT disable, skip" in p


def test_run_fix_includes_preexisting_from_cfg(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = Config()
    cfg.maven.test_excludes = ["FooTest#bar"]

    captured = {}

    def fake_runner(args, **kw):
        captured["prompt"] = args[-1]
        return ProcResult(args=list(args), returncode=0, stdout="", stderr="")

    run_fix(cfg, _item(), "log", build_cmd="mvn verify", runner=fake_runner)
    assert "FooTest#bar" in captured["prompt"]
    assert "ALREADY failing" in captured["prompt"]


def test_codex_command_default_sandbox():
    cfg = Config()
    cmd = build_codex_command(cfg, "PROMPT")
    assert cmd[:4] == ["codex", "exec", "--cd", str(cfg.repo)]
    assert "--sandbox" in cmd and "workspace-write" in cmd
    assert "--ask-for-approval" in cmd and "never" in cmd
    assert cmd[-1] == "PROMPT"


def test_codex_command_bypass_mode():
    cfg = Config()
    cfg.codex.bypass_sandbox = True
    cmd = build_codex_command(cfg, "P")
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--sandbox" not in cmd


def test_run_fix_passes_env_and_redacts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secrettoken1234567890abcd")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-secrettoken1234567890abc")
    cfg = Config()

    captured = {}

    def fake_runner(args, **kw):
        captured["args"] = args
        captured["env"] = kw.get("env")
        return ProcResult(
            args=list(args), returncode=0,
            stdout="leaked glpat-secrettoken1234567890abc", stderr="",
        )

    res = run_fix(cfg, _item(), "log", build_cmd="mvn verify", runner=fake_runner)
    assert res.invoked and res.exit_code == 0
    assert captured["env"]["OPENAI_API_KEY"] == "sk-secrettoken1234567890abcd"
    assert "glpat-secrettoken1234567890abc" not in res.stdout
