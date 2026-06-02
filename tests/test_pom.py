import os
from pathlib import Path

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.models import Kind, VersionSource
from mvn_upgrader.pom import (
    build_raw_index,
    classify_artifacts,
    discover,
    discover_module_poms,
    parse_effective_poms,
    resolve_effective_from_raw,
)

FIX = Path(__file__).parent / "fixtures"


def _artifacts_for(repo: Path):
    index = build_raw_index(repo)
    modules = resolve_effective_from_raw(index)
    arts = classify_artifacts(modules, index)
    return {(a.kind, a.ga): a for a in arts}


def test_single_module_classification():
    arts = _artifacts_for(FIX / "single")

    guava = arts[(Kind.DEPENDENCY, "com.google.guava:guava")]
    assert guava.version_source == VersionSource.PROPERTY
    assert guava.property_name == "guava.version"
    assert guava.current_version == "32.0.0-jre"

    lang3 = arts[(Kind.DEPENDENCY, "org.apache.commons:commons-lang3")]
    assert lang3.version_source == VersionSource.MANAGED
    assert lang3.current_version == "3.12.0"

    joda = arts[(Kind.DEPENDENCY, "joda-time:joda-time")]
    assert joda.version_source == VersionSource.LITERAL
    assert joda.current_version == "2.12.5"

    # version supplied by external parent -> managed-external (no in-repo decl)
    web = arts[(Kind.DEPENDENCY, "org.springframework.boot:spring-boot-starter-web")]
    assert web.version_source == VersionSource.MANAGED
    assert web.declared_in is None

    surefire = arts[(Kind.PLUGIN, "org.apache.maven.plugins:maven-surefire-plugin")]
    assert surefire.version_source == VersionSource.LITERAL
    assert surefire.current_version == "3.0.0"

    parent = arts[(Kind.DEPENDENCY, "org.springframework.boot:spring-boot-starter-parent")]
    assert parent.version_source == VersionSource.PARENT
    assert parent.current_version == "3.1.0"


def test_multi_module_classification():
    arts = _artifacts_for(FIX / "multi")

    jackson = arts[(Kind.DEPENDENCY, "com.fasterxml.jackson.core:jackson-databind")]
    assert jackson.version_source == VersionSource.PROPERTY
    assert jackson.property_name == "jackson.version"
    assert jackson.current_version == "2.15.0"

    commons_io = arts[(Kind.DEPENDENCY, "org.apache.commons:commons-io")]
    assert commons_io.version_source == VersionSource.LITERAL
    assert commons_io.current_version == "2.11.0"

    # internal reactor module must NOT appear as an upgrade target
    assert (Kind.DEPENDENCY, "com.example:mod-b") not in arts
    # internal parent must NOT appear as a parent upgrade target
    assert (Kind.DEPENDENCY, "com.example:parent") not in arts


def test_module_discovery_walks_reactor():
    poms = discover_module_poms(FIX / "multi")
    names = sorted(os.path.relpath(p, FIX / "multi") for p in poms)
    assert names == ["mod-a/pom.xml", "mod-b/pom.xml", "pom.xml"]


def test_parse_effective_pom_projects_wrapper():
    xml = """<?xml version="1.0"?>
    <projects>
      <project xmlns="http://maven.apache.org/POM/4.0.0">
        <groupId>com.example</groupId>
        <artifactId>app</artifactId>
        <version>1.0.0</version>
        <dependencies>
          <dependency>
            <groupId>com.google.guava</groupId>
            <artifactId>guava</artifactId>
            <version>32.0.0-jre</version>
          </dependency>
        </dependencies>
        <build>
          <plugins>
            <plugin>
              <groupId>org.apache.maven.plugins</groupId>
              <artifactId>maven-surefire-plugin</artifactId>
              <version>3.0.0</version>
            </plugin>
          </plugins>
        </build>
      </project>
    </projects>
    """
    mods = parse_effective_poms(xml)
    assert len(mods) == 1
    assert mods[0].ga == "com.example:app"
    assert mods[0].dependencies[0].version == "32.0.0-jre"
    assert mods[0].plugins[0].artifact_id == "maven-surefire-plugin"


def test_parse_effective_pom_single_project():
    xml = """<project xmlns="http://maven.apache.org/POM/4.0.0">
      <groupId>g</groupId><artifactId>a</artifactId><version>1.0</version>
    </project>"""
    mods = parse_effective_poms(xml)
    assert len(mods) == 1 and mods[0].version == "1.0"


def test_discover_uses_fallback_without_maven():
    cfg = Config(repo_path=str(FIX / "single"))
    result = discover(cfg)
    assert result.used_fallback is True
    gas = {a.ga for a in result.artifacts}
    assert "com.google.guava:guava" in gas
    # sorted: plugins come before? sorted by (kind, ga); dependency < plugin
    assert result.artifacts == sorted(
        result.artifacts, key=lambda a: (a.kind.value, a.ga)
    )
