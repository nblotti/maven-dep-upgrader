import sys
from io import StringIO

from mvn_upgrader.config import Config
from mvn_upgrader import runlog


def test_run_log_tee_and_path(tmp_path):
    cfg = Config(repo_path=str(tmp_path))
    path = runlog.activate(cfg)
    assert path == tmp_path / ".mvn-upgrade-work" / "run.log"
    assert path.is_file()

    print("hello progress")
    print("second line", file=sys.stderr)

    runlog.deactivate()

    text = path.read_text(encoding="utf-8")
    assert "mvn-upgrade run log started" in text
    assert "hello progress" in text
    assert "second line" in text
    assert "run log ended" in text


def test_custom_log_file(tmp_path):
    cfg = Config(repo_path=str(tmp_path))
    cfg.run.log_file = "logs/my-run.log"
    path = runlog.run_log_path(cfg)
    assert path == tmp_path / "logs" / "my-run.log"

    runlog.activate(cfg)
    print("custom location")
    runlog.deactivate()
    assert (tmp_path / "logs" / "my-run.log").read_text().find("custom location") >= 0
