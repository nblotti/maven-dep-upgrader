from mvn_upgrader.git_status import is_ignored_tool_path, user_dirty_lines


def test_ignores_mvn_upgrade_workdir():
    porcelain = "?? .mvn-upgrade-work/run.log\n?? .mvn-upgrade-work/\n"
    assert user_dirty_lines(porcelain) == []
    assert is_ignored_tool_path(".mvn-upgrade-work/run.log")
    assert is_ignored_tool_path(".mvn-upgrade-work")


def test_detects_real_changes():
    porcelain = "?? .mvn-upgrade-work/run.log\n M pom.xml\n"
    dirty = user_dirty_lines(porcelain)
    assert len(dirty) == 1
    assert "pom.xml" in dirty[0]
