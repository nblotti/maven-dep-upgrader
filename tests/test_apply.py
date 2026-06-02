from pathlib import Path

import pytest

from mvn_upgrader.apply import (
    apply_via_text_edit,
    build_mvn_command,
    edit_artifact_version,
    edit_parent_version,
    edit_property,
)
from mvn_upgrader.config import Config
from mvn_upgrader.models import Artifact, Kind, PlanItem, VersionSource


def _item(version_source, kind=Kind.DEPENDENCY, prop=None, declared_in="pom.xml",
          ga="com.example:lib", current="1.0.0", target="1.1.0"):
    g, a = ga.split(":")
    art = Artifact(
        group_id=g, artifact_id=a, current_version=current, kind=kind,
        version_source=version_source, declared_in=declared_in, property_name=prop,
    )
    return PlanItem(artifact=art, target_version=target)


def test_build_mvn_command_property():
    cfg = Config()
    cmd = build_mvn_command(cfg, _item(VersionSource.PROPERTY, prop="guava.version"))
    assert "versions:set-property" in cmd
    assert "-Dproperty=guava.version" in cmd
    assert "-DnewVersion=1.1.0" in cmd
    assert "-DgenerateBackupPoms=false" in cmd


def test_build_mvn_command_parent():
    cmd = build_mvn_command(Config(), _item(VersionSource.PARENT))
    assert "versions:update-parent" in cmd
    assert "-DparentVersion=[1.1.0]" in cmd


def test_build_mvn_command_literal_dependency():
    cmd = build_mvn_command(Config(), _item(VersionSource.LITERAL))
    assert "versions:use-dep-version" in cmd
    assert "-Dincludes=com.example:lib" in cmd
    assert "-DdepVersion=1.1.0" in cmd
    assert "-DforceVersion=true" in cmd


def test_build_mvn_command_plugin_returns_none():
    cmd = build_mvn_command(Config(), _item(VersionSource.LITERAL, kind=Kind.PLUGIN))
    assert cmd is None


def test_build_mvn_command_includes_settings():
    cfg = Config()
    cfg.maven.settings = "/tmp/settings.xml"
    cmd = build_mvn_command(cfg, _item(VersionSource.LITERAL))
    assert "-s" in cmd and "/tmp/settings.xml" in cmd


def test_edit_property():
    text = "<properties>\n  <guava.version>32.0.0-jre</guava.version>\n</properties>"
    new, n = edit_property(text, "guava.version", "32.1.0-jre")
    assert n == 1
    assert "<guava.version>32.1.0-jre</guava.version>" in new


def test_edit_artifact_version_literal():
    text = """<dependency>
      <groupId>joda-time</groupId>
      <artifactId>joda-time</artifactId>
      <version>2.12.5</version>
    </dependency>"""
    new, n = edit_artifact_version(text, "joda-time", "joda-time", "2.12.7")
    assert n == 1 and "<version>2.12.7</version>" in new


def test_edit_artifact_version_skips_property():
    text = """<dependency>
      <groupId>g</groupId><artifactId>a</artifactId>
      <version>${x.version}</version>
    </dependency>"""
    new, n = edit_artifact_version(text, "g", "a", "2.0.0")
    assert n == 0 and "${x.version}" in new


def test_edit_artifact_version_only_matching_ga():
    text = """<dependency><groupId>g</groupId><artifactId>a</artifactId>
      <version>1.0</version></dependency>
      <dependency><groupId>g</groupId><artifactId>b</artifactId>
      <version>2.0</version></dependency>"""
    new, n = edit_artifact_version(text, "g", "a", "9.9")
    assert n == 1
    assert "<version>9.9</version>" in new
    assert "<version>2.0</version>" in new  # b untouched


def test_edit_parent_version():
    text = """<parent>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-parent</artifactId>
      <version>3.1.0</version>
    </parent>"""
    new, n = edit_parent_version(
        text, "org.springframework.boot", "spring-boot-starter-parent", "3.2.0"
    )
    assert n == 1 and "<version>3.2.0</version>" in new


def test_apply_via_text_edit_on_fixture(tmp_path):
    src = (Path(__file__).parent / "fixtures" / "single" / "pom.xml").read_text()
    pom = tmp_path / "pom.xml"
    pom.write_text(src)

    item = _item(VersionSource.LITERAL, ga="joda-time:joda-time",
                 declared_in=str(pom), current="2.12.5", target="2.12.7")
    res = apply_via_text_edit(item)
    assert res.ok and res.via == "text-edit"
    assert "<version>2.12.7</version>" in pom.read_text()


def test_apply_via_text_edit_property_on_fixture(tmp_path):
    src = (Path(__file__).parent / "fixtures" / "single" / "pom.xml").read_text()
    pom = tmp_path / "pom.xml"
    pom.write_text(src)

    item = _item(VersionSource.PROPERTY, prop="guava.version",
                 ga="com.google.guava:guava", declared_in=str(pom),
                 current="32.0.0-jre", target="32.1.0-jre")
    res = apply_via_text_edit(item)
    assert res.ok
    assert "<guava.version>32.1.0-jre</guava.version>" in pom.read_text()


def test_apply_via_text_edit_no_declaring_pom():
    item = _item(VersionSource.MANAGED, declared_in=None)
    res = apply_via_text_edit(item)
    assert res.ok is False
