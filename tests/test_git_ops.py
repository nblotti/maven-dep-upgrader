import subprocess
from pathlib import Path

import pytest

from mvn_upgrader.git_ops import Git, GitError


def _init_repo(path: Path) -> Git:
    def run(args, **kw):
        return subprocess.run(args, cwd=path, capture_output=True, text=True)

    run(["git", "init", "-q"])
    run(["git", "config", "user.email", "t@example.com"])
    run(["git", "config", "user.name", "Test"])
    (path / "pom.xml").write_text("<project><version>1.0</version></project>")
    run(["git", "add", "-A"])
    run(["git", "commit", "-q", "-m", "init"])
    return Git(path)


def test_clean_and_changes(tmp_path):
    g = _init_repo(tmp_path)
    assert g.is_repo()
    assert g.is_clean()
    (tmp_path / "pom.xml").write_text("<project><version>2.0</version></project>")
    assert not g.is_clean()
    assert "pom.xml" in g.changed_files()


def test_checkpoint_commit_restore(tmp_path):
    g = _init_repo(tmp_path)
    base = g.checkpoint()

    (tmp_path / "pom.xml").write_text("<project><version>2.0</version></project>")
    sha = g.commit_all("build(deps): bump example from 1.0 to 2.0")
    assert sha and sha != base
    assert g.is_clean()

    # simulate a failed item: change + untracked file, then restore
    (tmp_path / "pom.xml").write_text("<project><version>3.0</version></project>")
    (tmp_path / "junk.txt").write_text("garbage from codex")
    g.restore_to(sha)
    assert g.is_clean()
    assert not (tmp_path / "junk.txt").exists()
    assert "2.0" in (tmp_path / "pom.xml").read_text()


def test_create_branch(tmp_path):
    g = _init_repo(tmp_path)
    g.create_branch("chore/dependency-upgrades/2026-06-02")
    assert g.current_branch() == "chore/dependency-upgrades/2026-06-02"


def test_commit_nothing_returns_none(tmp_path):
    g = _init_repo(tmp_path)
    assert g.commit_all("build(deps): noop") is None
