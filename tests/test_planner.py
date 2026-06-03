from pathlib import Path

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.models import (
    Artifact,
    Candidate,
    Kind,
    Status,
    VersionSource,
)
from mvn_upgrader.planner import build_plan
from mvn_upgrader.pom import build_raw_index, classify_artifacts, resolve_effective_from_raw

FIX = Path(__file__).parent / "fixtures"


class FakeNexus:
    def __init__(self, mapping):
        self.mapping = mapping

    def list_versions(self, group_id, artifact_id, **kwargs):
        ga = f"{group_id}:{artifact_id}"
        return [Candidate(v, "repo") for v in self.mapping.get(ga, [])]


def _artifacts(repo: Path):
    index = build_raw_index(repo)
    modules = resolve_effective_from_raw(index)
    return classify_artifacts(modules, index)


def test_plan_single_module():
    cfg = Config(repo_path=str(FIX / "single"))
    arts = _artifacts(FIX / "single")
    nexus = FakeNexus({
        "com.google.guava:guava": ["32.0.0-jre", "32.1.0-jre", "33.0.0-jre"],
        "joda-time:joda-time": ["2.12.5", "2.12.7"],
        "org.apache.commons:commons-lang3": ["3.12.0", "3.14.0"],
        "org.apache.maven.plugins:maven-surefire-plugin": ["3.0.0", "3.2.5"],
        "org.springframework.boot:spring-boot-starter-parent":
            ["3.1.0", "3.1.5", "3.2.0"],
    })
    plan, results = build_plan(cfg, arts, nexus)

    targets = {i.ga: i.target_version for i in plan}
    assert targets["com.google.guava:guava"] == "32.1.0-jre"  # no major jump to 33
    assert targets["joda-time:joda-time"] == "2.12.7"
    assert targets["org.apache.commons:commons-lang3"] == "3.14.0"
    assert targets["org.apache.maven.plugins:maven-surefire-plugin"] == "3.2.5"
    assert targets["org.springframework.boot:spring-boot-starter-parent"] == "3.2.0"

    # Parent POM upgrade runs after deps and plugins.
    assert plan[-1].ga == "org.springframework.boot:spring-boot-starter-parent"
    assert plan[-1].artifact.version_source == VersionSource.PARENT

    by_ga = {(r.ga, r.status) for r in results}
    assert ("org.springframework.boot:spring-boot-starter-web",
            Status.MANAGED_EXTERNAL) in by_ga


def test_plan_parent_can_run_mixed_order():
    cfg = Config(repo_path=str(FIX / "single"))
    cfg.policy.parent_last = False
    arts = _artifacts(FIX / "single")
    nexus = FakeNexus({
        "com.google.guava:guava": ["32.0.0-jre", "32.1.0-jre"],
        "joda-time:joda-time": ["2.12.5", "2.12.7"],
        "org.apache.commons:commons-lang3": ["3.12.0", "3.14.0"],
        "org.apache.maven.plugins:maven-surefire-plugin": ["3.0.0", "3.2.5"],
        "org.springframework.boot:spring-boot-starter-parent": ["3.1.0", "3.2.0"],
    })
    plan, _ = build_plan(cfg, arts, nexus)
    assert plan[-1].ga != "org.springframework.boot:spring-boot-starter-parent"


def test_plan_not_in_nexus_and_no_newer():
    cfg = Config(repo_path=str(FIX / "single"))
    arts = _artifacts(FIX / "single")
    nexus = FakeNexus({
        "joda-time:joda-time": ["2.12.5"],  # nothing newer
        # everything else absent -> not-in-nexus
    })
    plan, results = build_plan(cfg, arts, nexus)
    statuses = {r.ga: r.status for r in results}
    assert statuses["joda-time:joda-time"] == Status.SKIPPED_NO_NEWER
    assert statuses["com.google.guava:guava"] == Status.NOT_IN_NEXUS


def test_include_exclude_filtering():
    cfg = Config(repo_path=str(FIX / "single"))
    cfg.policy.include = ["joda-time:*"]
    arts = _artifacts(FIX / "single")
    nexus = FakeNexus({"joda-time:joda-time": ["2.12.5", "2.13.0"]})
    plan, results = build_plan(cfg, arts, nexus)
    assert [i.ga for i in plan] == ["joda-time:joda-time"]
    # excluded artifacts are not reported at all
    assert all(r.ga.startswith("joda-time") for r in results)


def _prop_artifact(ga, version):
    g, a = ga.split(":")
    return Artifact(
        group_id=g, artifact_id=a, current_version=version,
        kind=Kind.DEPENDENCY, version_source=VersionSource.PROPERTY,
        declared_in="pom.xml", property_name="jackson.version",
    )


def test_shared_property_one_plan_item_with_common_version():
    cfg = Config()
    arts = [
        _prop_artifact("com.fasterxml.jackson.core:jackson-core", "2.15.0"),
        _prop_artifact("com.fasterxml.jackson.core:jackson-databind", "2.15.0"),
    ]
    nexus = FakeNexus({
        "com.fasterxml.jackson.core:jackson-core": ["2.15.0", "2.16.0", "2.17.0"],
        "com.fasterxml.jackson.core:jackson-databind": ["2.15.0", "2.16.0"],
    })
    plan, results = build_plan(cfg, arts, nexus)
    assert len(plan) == 1
    item = plan[0]
    # 2.17.0 not available for databind -> highest common is 2.16.0
    assert item.target_version == "2.16.0"
    assert item.co_moved == ["com.fasterxml.jackson.core:jackson-databind"]


def test_shared_property_one_at_a_time_skips():
    cfg = Config()
    cfg.policy.one_at_a_time = True
    arts = [
        _prop_artifact("com.fasterxml.jackson.core:jackson-core", "2.15.0"),
        _prop_artifact("com.fasterxml.jackson.core:jackson-databind", "2.15.0"),
    ]
    nexus = FakeNexus({
        "com.fasterxml.jackson.core:jackson-core": ["2.15.0", "2.16.0"],
        "com.fasterxml.jackson.core:jackson-databind": ["2.15.0", "2.16.0"],
    })
    plan, results = build_plan(cfg, arts, nexus)
    assert plan == []
    assert all(r.status == Status.INFORMATIONAL for r in results)
