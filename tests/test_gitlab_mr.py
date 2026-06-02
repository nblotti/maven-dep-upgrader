from pathlib import Path

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.gitlab_mr import (
    build_glab_command,
    create_mr_via_glab,
    create_mr_via_rest,
)
from mvn_upgrader.proc import ProcResult


def _cfg():
    cfg = Config()
    cfg.gitlab.project = "group/sub/repo"
    cfg.git.base_branch = "main"
    return cfg


def test_build_glab_command():
    cmd = build_glab_command(_cfg(), "chore/x", Path("/tmp/r.md"), use_file=True)
    assert cmd[:3] == ["glab", "mr", "create"]
    assert "--source-branch" in cmd and "chore/x" in cmd
    assert "--target-branch" in cmd and "main" in cmd
    assert "--yes" in cmd
    assert "-R" in cmd and "group/sub/repo" in cmd
    assert "--description-file" in cmd and "/tmp/r.md" in cmd


def test_create_mr_via_glab_parses_url(tmp_path):
    desc = tmp_path / "r.md"
    desc.write_text("body")
    url = "https://gitlab.com/group/sub/repo/-/merge_requests/42"

    def runner(args, **kw):
        return ProcResult(args=list(args), returncode=0,
                          stdout=f"Creating MR\n{url}\n", stderr="")

    assert create_mr_via_glab(_cfg(), "chore/x", desc, runner=runner) == url


def test_create_mr_via_glab_falls_back_to_description(tmp_path):
    desc = tmp_path / "r.md"
    desc.write_text("body text")
    url = "https://gitlab.com/g/r/-/merge_requests/7"
    seen = []

    def runner(args, **kw):
        seen.append(args)
        if "--description-file" in args:
            return ProcResult(args=list(args), returncode=1,
                              stdout="", stderr="unknown flag")
        return ProcResult(args=list(args), returncode=0, stdout=url, stderr="")

    out = create_mr_via_glab(_cfg(), "chore/x", desc, runner=runner)
    assert out == url
    assert any("--description" in a and "--description-file" not in a for a in seen)


class FakeResp:
    def __init__(self, data, status=201):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResp({"web_url": "https://gitlab.com/g/r/-/merge_requests/9"})


def test_create_mr_via_rest(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-abcdefghijklmnop12345")
    cfg = _cfg()
    sess = FakeSession()
    url = create_mr_via_rest(cfg, "chore/x", "the body", session=sess)
    assert url == "https://gitlab.com/g/r/-/merge_requests/9"
    call = sess.calls[0]
    # project path is URL-encoded
    assert "group%2Fsub%2Frepo" in call["url"]
    assert call["headers"]["PRIVATE-TOKEN"] == "glpat-abcdefghijklmnop12345"
    assert call["json"]["source_branch"] == "chore/x"
    assert call["json"]["target_branch"] == "main"


def test_create_mr_via_rest_self_managed_host(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    cfg = _cfg()
    cfg.gitlab.host = "gitlab.internal.example.com"
    sess = FakeSession()
    create_mr_via_rest(cfg, "b", "d", session=sess)
    assert sess.calls[0]["url"].startswith("https://gitlab.internal.example.com/api/v4/")
