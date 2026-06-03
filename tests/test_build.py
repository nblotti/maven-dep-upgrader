from pathlib import Path

import pytest

from mvn_upgrader.build import (
    build_command,
    effective_build_command,
    error_signature,
    normalize_error_lines,
    run,
)
from mvn_upgrader.config import Config
from mvn_upgrader.proc import ProcResult


LOG_A = """
[INFO] Building app 1.0.0
2026-06-02 10:00:00 [ERROR] /home/me/proj/src/main/java/A.java:[42,15] cannot find symbol
[ERROR]   symbol:   method oldApi()
[ERROR] BUILD FAILURE
"""

# Same error, different absolute path, timestamp, and line numbers.
LOG_A_VARIANT = """
[INFO] Building app 1.0.0
2026-06-02 11:22:33 [ERROR] /opt/ci/work/123/src/main/java/A.java:[99,3] cannot find symbol
[ERROR]   symbol:   method oldApi()
[ERROR] BUILD FAILURE
"""

LOG_B = """
[ERROR] /x/B.java:[1,1] incompatible types: String cannot be converted to int
[ERROR] BUILD FAILURE
"""


def test_signature_stable_across_paths_and_numbers():
    assert error_signature(LOG_A) == error_signature(LOG_A_VARIANT)


def test_signature_differs_for_different_errors():
    assert error_signature(LOG_A) != error_signature(LOG_B)


def test_signature_none_when_no_errors():
    assert error_signature("[INFO] BUILD SUCCESS") is None


def test_normalize_strips_banner_only_lines():
    lines = normalize_error_lines("[ERROR] BUILD FAILURE")
    assert lines == []


def test_build_command_inserts_settings():
    cfg = Config()
    cfg.maven.settings = "/tmp/s.xml"
    cmd = build_command(cfg)
    assert cmd[0] == "mvn"
    assert cmd[1] == "-s" and cmd[2] == "/tmp/s.xml"


def test_effective_build_command_includes_settings():
    cfg = Config()
    cfg.maven.settings = "/tmp/s.xml"
    s = effective_build_command(cfg)
    assert "-s /tmp/s.xml" in s or s.endswith("/tmp/s.xml")


def test_build_command_default_when_empty():
    cfg = Config()
    cfg.maven.build_command = ""
    cmd = build_command(cfg)
    assert "clean" in cmd and "verify" in cmd


def test_build_command_injects_test_excludes():
    cfg = Config()
    cfg.maven.test_excludes = ["FooTest#bar", "BazTest"]
    cmd = build_command(cfg)
    assert "-Dtest=!FooTest#bar,!BazTest" in cmd
    assert "-Dit.test=!FooTest#bar,!BazTest" in cmd
    assert "-Dsurefire.failIfNoSpecifiedTests=false" in cmd
    assert "-Dfailsafe.failIfNoSpecifiedTests=false" in cmd


def test_build_command_does_not_clobber_existing_dtest():
    cfg = Config()
    cfg.maven.build_command = "mvn -B verify -Dtest=OnlyThis"
    cfg.maven.test_excludes = ["FooTest#bar"]
    cmd = build_command(cfg)
    # user's explicit -Dtest is preserved; no injected exclusion
    assert "-Dtest=OnlyThis" in cmd
    assert not any(c.startswith("-Dtest=!") for c in cmd)


def test_build_command_no_excludes_by_default():
    cmd = build_command(Config())
    assert not any(c.startswith("-Dtest=") for c in cmd)


def test_run_writes_log_and_tail(tmp_path):
    cfg = Config(repo_path=str(tmp_path))

    def fake_runner(args, **kw):
        return ProcResult(args=list(args), returncode=1, stdout=LOG_A, stderr="")

    res = run(cfg, attempt_tag="bump-1", runner=fake_runner)
    assert res.ok is False and res.exit_code == 1
    assert res.error_signature is not None
    assert Path(res.log_path).is_file()
    assert "cannot find symbol" in res.tail


def test_run_redacts_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-abcdefghijklmnopqrstuvwx")
    cfg = Config(repo_path=str(tmp_path))

    leaky = "[ERROR] failed with token glpat-abcdefghijklmnopqrstuvwx in log"

    def fake_runner(args, **kw):
        return ProcResult(args=list(args), returncode=1, stdout=leaky, stderr="")

    res = run(cfg, runner=fake_runner)
    assert "glpat-abcdefghijklmnopqrstuvwx" not in res.tail
    assert "glpat-abcdefghijklmnopqrstuvwx" not in Path(res.log_path).read_text()
