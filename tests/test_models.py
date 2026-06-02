from mvn_upgrader.models import (
    Artifact,
    Kind,
    Status,
    UpgradeResult,
    VersionSource,
)


def _artifact():
    return Artifact(
        group_id="com.example",
        artifact_id="lib",
        current_version="1.0.0",
        kind=Kind.DEPENDENCY,
        version_source=VersionSource.LITERAL,
        declared_in="pom.xml",
    )


def test_artifact_ga_and_dict():
    a = _artifact()
    assert a.ga == "com.example:lib"
    d = a.to_dict()
    assert d["kind"] == "dependency"
    assert d["version_source"] == "literal"


def test_upgrade_result_roundtrip():
    r = UpgradeResult(
        group_id="com.example",
        artifact_id="lib",
        kind=Kind.DEPENDENCY,
        version_source=VersionSource.PROPERTY,
        old_version="1.0.0",
        new_version="1.1.0",
        status=Status.UPGRADED,
        commit="abc123",
        fix_attempts=2,
        notes=["ok"],
    )
    d = r.to_dict()
    r2 = UpgradeResult.from_dict(d)
    assert r2 == r
    assert d["status"] == "upgraded"


def test_informational_factory():
    r = UpgradeResult.informational(_artifact(), "pure transitive")
    assert r.status == Status.INFORMATIONAL
    assert r.new_version is None
    assert "pure transitive" in r.notes
