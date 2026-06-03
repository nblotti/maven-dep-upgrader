from pathlib import Path

import pytest

from mvn_upgrader.models import Artifact, Kind, PlanItem, VersionSource
from mvn_upgrader.upgrade_plan import (
    PlanFileError,
    export_plan_csv,
    load_plan_csv,
    resolve_plan_from_file,
)


def _item(ga=("com.example", "lib"), target="2.0.0", **kw):
    gid, aid = ga
    art = Artifact(
        group_id=gid, artifact_id=aid, current_version="1.0.0",
        kind=Kind.DEPENDENCY, version_source=VersionSource.LITERAL,
        declared_in="pom.xml", **kw,
    )
    return PlanItem(artifact=art, target_version=target)


def test_export_and_load_roundtrip(tmp_path):
    from mvn_upgrader.config import Config

    cfg = Config(repo_path=str(tmp_path))
    plan = [_item(), _item(ga=("com.other", "x"), target="3.0.0")]
    path = export_plan_csv(cfg, plan)
    rows = load_plan_csv(path)
    assert len(rows) == 2
    assert rows[0].order == 1 and rows[1].order == 2


def test_resolve_skips_order_zero(tmp_path):
    from mvn_upgrader.config import Config

    cfg = Config(repo_path=str(tmp_path))
    plan = [_item(), _item(ga=("com.other", "x"), target="3.0.0")]
    path = export_plan_csv(cfg, plan)
    text = path.read_text(encoding="utf-8").replace(
        "com.example,lib", "com.example,lib", 1
    )
    # Set second row order to 0
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace("2,", "0,", 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    batches = resolve_plan_from_file(plan, load_plan_csv(path))
    assert len(batches) == 1
    assert len(batches[0]) == 1
    assert batches[0][0].ga == "com.example:lib"


def test_resolve_same_order_batches(tmp_path):
    from mvn_upgrader.config import Config

    cfg = Config(repo_path=str(tmp_path))
    plan = [_item(), _item(ga=("com.other", "x"), target="3.0.0")]
    path = export_plan_csv(cfg, plan)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace("2,", "1,", 1)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    batches = resolve_plan_from_file(plan, load_plan_csv(path))
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_resolve_custom_target_version(tmp_path):
    from mvn_upgrader.config import Config

    cfg = Config(repo_path=str(tmp_path))
    plan = [_item(target="2.0.0")]
    path = export_plan_csv(cfg, plan)
    text = path.read_text(encoding="utf-8").replace("2.0.0", "2.5.0")
    path.write_text(text, encoding="utf-8")

    batches = resolve_plan_from_file(plan, load_plan_csv(path))
    assert batches[0][0].target_version == "2.5.0"


def test_load_missing_file(tmp_path):
    with pytest.raises(PlanFileError):
        load_plan_csv(tmp_path / "nope.csv")
