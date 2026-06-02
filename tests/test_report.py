from pathlib import Path

import pytest

from mvn_upgrader.config import Config
from mvn_upgrader.models import Kind, Status, UpgradeResult, VersionSource
from mvn_upgrader.report import (
    RunState,
    load_state,
    regenerate_from_state,
    render_markdown,
    write_reports,
)


def _results():
    return [
        UpgradeResult("g", "a", Kind.DEPENDENCY, VersionSource.LITERAL,
                      "1.0", "1.1", Status.UPGRADED, commit="abcdef1234567",
                      fix_attempts=1),
        UpgradeResult("g", "b", Kind.DEPENDENCY, VersionSource.PROPERTY,
                      "2.0", "2.1", Status.SKIPPED_BUILD_FAILED,
                      notes=["error signature deadbeef"]),
        UpgradeResult("g", "c", Kind.PLUGIN, VersionSource.MANAGED,
                      "3.0", None, Status.NOT_IN_NEXUS),
        UpgradeResult("ext", "parent", Kind.DEPENDENCY, VersionSource.MANAGED,
                      None, None, Status.MANAGED_EXTERNAL,
                      notes=["upgrade parent"]),
        UpgradeResult("g", "trans", Kind.DEPENDENCY, VersionSource.NONE,
                      None, None, Status.INFORMATIONAL, notes=["pure transitive"]),
    ]


def test_render_markdown_sections():
    state = RunState(generated_at="2026-06-02T00:00:00Z", mode="run",
                     branch="chore/x", base_branch="main", results=_results())
    md = render_markdown(state)
    assert "# Dependency updates" in md
    assert "## Summary" in md
    assert "upgraded: 1" in md
    assert "| Artifact | Kind | Old | New | Source | Status | Commit |" in md
    assert "g:a" in md and "abcdef1234" in md
    assert "## Skipped / failed" in md
    assert "g:b" in md
    assert "## Informational" in md
    assert "g:trans" in md
    # informational rows are not in the main table
    main_table = md.split("## Skipped")[0]
    assert "g:trans" not in main_table


def test_write_and_reload_state(tmp_path):
    cfg = Config(repo_path=str(tmp_path))
    state = RunState(generated_at="t", mode="run", base_branch="main",
                     results=_results())
    md_path, json_path = write_reports(cfg, state)
    assert md_path.is_file() and json_path.is_file()

    reloaded = load_state(cfg)
    assert reloaded is not None
    assert len(reloaded.results) == 5
    assert reloaded.results[0].status == Status.UPGRADED


def test_regenerate_from_state(tmp_path):
    cfg = Config(repo_path=str(tmp_path))
    state = RunState(generated_at="t", mode="run", base_branch="main",
                     results=_results())
    write_reports(cfg, state)
    (cfg.repo / "dependency-updates.md").unlink()
    out = regenerate_from_state(cfg)
    assert out is not None and out.is_file()


def test_regenerate_no_state(tmp_path):
    cfg = Config(repo_path=str(tmp_path))
    assert regenerate_from_state(cfg) is None
