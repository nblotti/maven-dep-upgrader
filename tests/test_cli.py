import pytest

from mvn_upgrader.cli import build_parser, _split_csv


def test_parser_plan():
    args = build_parser().parse_args(["plan", "--config", "c.yaml"])
    assert args.command == "plan"
    assert args.config == "c.yaml"


def test_parser_run_flags():
    args = build_parser().parse_args(
        ["run", "--apply", "--create-mr", "--only", "g:a,h:b", "--max", "3"]
    )
    assert args.apply is True
    assert args.create_mr is True
    assert args.max == 3
    assert _split_csv(args.only) == ["g:a", "h:b"]


def test_parser_baseline_flag():
    args = build_parser().parse_args(["run", "--apply", "--baseline", "skip-failing"])
    assert args.baseline == "skip-failing"
    # default is None (falls back to config)
    args2 = build_parser().parse_args(["run", "--apply"])
    assert args2.baseline is None


def test_parser_baseline_rejects_unknown():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--baseline", "bogus"])


def test_parser_baseline_fix_codex():
    args = build_parser().parse_args(["run", "--apply", "--baseline", "fix-codex"])
    assert args.baseline == "fix-codex"


def test_parser_requires_command():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_split_csv_empty():
    assert _split_csv(None) == []
    assert _split_csv("") == []
