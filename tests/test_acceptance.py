"""Acceptance-level checks tying the read-only pipeline together."""

import shutil
import subprocess
from pathlib import Path

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.models import Candidate
from mvn_upgrader.git_ops import Git

FIX = Path(__file__).parent / "fixtures"


class FakeNexus:
    def __init__(self, mapping):
        self.mapping = mapping

    def list_versions(self, g, a):
        return [Candidate(v, "maven-public") for v in self.mapping.get(f"{g}:{a}", [])]


def _init_repo(path: Path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=path)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path)
    subprocess.run(["git", "add", "-A"], cwd=path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path)


def test_plan_makes_zero_source_changes(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    shutil.copytree(FIX / "single", repo)
    _init_repo(repo)

    cfg = Config(repo_path=str(repo))
    cfg.nexus.base_url = "https://nexus.example.com"
    cfg.nexus.repositories = ["maven-public"]

    fake = FakeNexus({
        "com.google.guava:guava": ["32.0.0-jre", "32.1.0-jre"],
        "joda-time:joda-time": ["2.12.5", "2.12.7"],
        "org.apache.commons:commons-lang3": ["3.12.0", "3.14.0"],
        "org.apache.maven.plugins:maven-surefire-plugin": ["3.0.0", "3.2.5"],
        "org.springframework.boot:spring-boot-starter-parent": ["3.1.0", "3.2.0"],
    })

    from mvn_upgrader import planner

    monkeypatch.setattr(planner.NexusClient, "from_config",
                        classmethod(lambda cls, c, session=None: fake))

    plan, results = planner.build_and_report_plan(cfg)
    assert len(plan) >= 4

    # The source pom.xml is untouched; only report files appear.
    g = Git(repo)
    pom = (repo / "pom.xml").read_text()
    assert "<version>2.12.5</version>" in pom  # joda NOT bumped by plan
    dirty = set(g.changed_files())
    assert dirty <= {"dependency-updates.md", "dependency-updates.json", "upgrade-plan.csv"}
    assert (repo / "dependency-updates.md").is_file()
    assert (repo / "upgrade-plan.csv").is_file()
