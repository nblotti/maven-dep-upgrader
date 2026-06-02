import subprocess
from pathlib import Path

import pytest

from mvn_upgrader.apply import ApplyResult
from mvn_upgrader.baseline import BaselineResult, FailingTest
from mvn_upgrader.build import BuildResult
from mvn_upgrader.config import Config
from mvn_upgrader.git_ops import Git
from mvn_upgrader.models import Artifact, Kind, PlanItem, Status, VersionSource
from mvn_upgrader.runner import _fix_loop, run_upgrades


def _init_repo(path: Path):
    def run(args):
        return subprocess.run(args, cwd=path, capture_output=True, text=True)

    run(["git", "init", "-q", "-b", "main"])
    run(["git", "config", "user.email", "t@example.com"])
    run(["git", "config", "user.name", "Test"])
    (path / "pom.xml").write_text("<project><version>1.0</version></project>\n")
    run(["git", "add", "-A"])
    run(["git", "commit", "-q", "-m", "init"])


def _item(target="2.0.0"):
    art = Artifact(
        group_id="com.example", artifact_id="lib", current_version="1.0.0",
        kind=Kind.DEPENDENCY, version_source=VersionSource.LITERAL,
        declared_in="pom.xml",
    )
    return PlanItem(artifact=art, target_version=target)


def _edit_pom_apply(pom: Path):
    def apply_fn(cfg, item):
        pom.write_text(f"<project><version>{item.target_version}</version></project>\n")
        return ApplyResult(True, "literal", "text-edit", "ok")
    return apply_fn


def _build_seq(results):
    it = iter(results)

    def build_fn(cfg, *, attempt_tag, runner=None):
        return next(it)
    return build_fn


def _cfg(repo: Path, **over):
    cfg = Config(repo_path=str(repo))
    cfg.codex.max_fix_attempts = 3
    # Legacy tests don't exercise the baseline build; keep it off by default.
    cfg.run.baseline = "off"
    for k, v in over.items():
        setattr(cfg.run, k, v) if hasattr(cfg.run, k) else None
    return cfg


# --------------------------------------------------------------------------- #
def test_fix_loop_no_progress_guard():
    cfg = Config()
    cfg.codex.max_fix_attempts = 4
    same = BuildResult(False, 1, None, "tail", "sig-identical")
    builds = _build_seq([same, same, same, same])
    calls = {"n": 0}

    def codex_fn(cfg, item, tail, *, build_cmd):
        calls["n"] += 1

    green, attempts, sig, _ = _fix_loop(cfg, _item(), build_fn=builds, codex_fn=codex_fn)
    assert green is False
    # build1 red -> codex(1) -> build2 same sig -> stop. Only one codex call.
    assert calls["n"] == 1
    assert attempts == 1


def test_fix_loop_no_progress_disabled_runs_all_attempts():
    cfg = Config()
    cfg.codex.max_fix_attempts = 4
    cfg.codex.stop_on_no_progress = False
    same = BuildResult(False, 1, None, "tail", "sig-identical")
    builds = _build_seq([same, same, same, same])
    calls = {"n": 0}

    def codex_fn(cfg, item, tail, *, build_cmd):
        calls["n"] += 1

    green, attempts, sig, _ = _fix_loop(cfg, _item(), build_fn=builds, codex_fn=codex_fn)
    assert green is False
    # 4 builds, codex after each of the first 3 (no early stop on identical sig).
    assert calls["n"] == 3
    assert attempts == 3


def test_green_first_try_commits(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        skip_preflight=True,
    )
    assert rc == 0
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "build(deps): bump com.example:lib from 1.0.0 to 2.0.0" in log
    assert "docs: add dependency-updates report" in log
    assert "2.0.0" in pom.read_text()


def test_red_then_codex_fixes(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)

    def codex_fn(cfg, item, tail, *, build_cmd):
        # pretend to fix something (still only pom is tracked)
        pom.write_text("<project><version>2.0.0</version><!--fixed--></project>\n")

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([
            BuildResult(False, 1, None, "boom", "sig1"),
            BuildResult(True, 0, None, "", None),
        ]),
        codex_fn=codex_fn,
        apply_fn=_edit_pom_apply(pom),
        skip_preflight=True,
    )
    assert rc == 0
    state = __import__("json").loads((tmp_path / "dependency-updates.json").read_text())
    res = state["results"][0]
    assert res["status"] == "upgraded"
    assert res["fix_attempts"] == 1


def test_persistent_red_reverts_clean(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    original = pom.read_text()
    cfg = _cfg(tmp_path)

    def codex_fn(cfg, item, tail, *, build_cmd):
        pom.write_text(pom.read_text() + "<!--x-->")

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([
            BuildResult(False, 1, None, "b", "sig1"),
            BuildResult(False, 1, None, "b", "sig2"),
            BuildResult(False, 1, None, "b", "sig3"),
        ]),
        codex_fn=codex_fn,
        apply_fn=_edit_pom_apply(pom),
        skip_preflight=True,
    )
    assert rc == 0
    g = Git(tmp_path)
    # The source POM is fully reverted; the only remaining changes are the
    # intentional report outputs.
    assert pom.read_text() == original  # reverted
    dirty = set(g.changed_files())
    assert dirty <= {"dependency-updates.json", "dependency-updates.md"}

    import json
    state = json.loads((tmp_path / "dependency-updates.json").read_text())
    assert state["results"][0]["status"] == "skipped-build-failed"


def test_create_mr_end_to_end(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.gitlab.project = "group/repo"

    # Avoid a real push; capture the MR creation.
    class NoPushGit(Git):
        def push(self, remote, branch, set_upstream=True):
            return None

    created = {}

    def fake_create_mr(cfg, *, branch, description_file, **kw):
        created["branch"] = branch
        created["desc"] = Path(description_file).read_text()
        return "https://gitlab.com/group/repo/-/merge_requests/5"

    import mvn_upgrader.gitlab_mr as gm
    monkeypatch.setattr(gm, "create_mr", fake_create_mr)

    rc = run_upgrades(
        cfg, create_mr=True, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        git_factory=NoPushGit,
        skip_preflight=True,
    )
    assert rc == 0
    assert created["branch"].startswith("chore/dependency-upgrades/")
    import json
    state = json.loads((tmp_path / "dependency-updates.json").read_text())
    assert state["mr_url"] == "https://gitlab.com/group/repo/-/merge_requests/5"


def test_resume_skips_already_upgraded(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)

    # First run: upgrade succeeds and is persisted to state.
    run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        skip_preflight=True,
    )
    log1 = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip().splitlines()

    # Second run with the same plan: the item is already upgraded -> skipped.
    applied = {"n": 0}

    def apply_fn(cfg, item):
        applied["n"] += 1
        return ApplyResult(True, "literal", "text-edit", "ok")

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([]),  # should never be called
        codex_fn=lambda *a, **k: None,
        apply_fn=apply_fn,
        skip_preflight=True,
    )
    assert rc == 0
    assert applied["n"] == 0  # nothing re-applied
    log2 = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip().splitlines()
    assert len(log2) == len(log1)  # no new commits


def _green_baseline(cfg):
    return BaselineResult(ok=True, failures=[],
                          build=BuildResult(True, 0, None, "", None))


def _red_baseline(failures):
    def fn(cfg):
        return BaselineResult(
            ok=False, failures=failures,
            build=BuildResult(False, 1, None, "tail", "sig"),
        )
    return fn


def test_baseline_green_proceeds(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "ask"  # but baseline is green, so no prompt

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=_green_baseline,
        skip_preflight=True,
    )
    assert rc == 0
    assert cfg.maven.test_excludes == []


def test_baseline_red_abort_stops(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    original = pom.read_text()
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "abort"

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=_red_baseline([FailingTest("com.x.FooTest", "bar")]),
        skip_preflight=True,
    )
    assert rc == 1
    assert pom.read_text() == original


def test_baseline_red_skip_failing_excludes_and_proceeds(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "skip-failing"

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=_red_baseline([
            FailingTest("com.x.FooTest", "bar"),
            FailingTest("com.y.BazTest", None),
        ]),
        skip_preflight=True,
    )
    assert rc == 0
    assert cfg.maven.test_excludes == ["BazTest", "FooTest#bar"]

    import json
    state = json.loads((tmp_path / "dependency-updates.json").read_text())
    assert state["baseline_excluded_tests"] == ["BazTest", "FooTest#bar"]
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "build(deps): bump com.example:lib" in log


def test_baseline_ask_prompt_no_skips(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "ask"

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(True, 0, None, "", None)]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=_red_baseline([FailingTest("com.x.FooTest", "bar")]),
        prompt_fn=lambda msg: "s",  # skip failing tests
        skip_preflight=True,
    )
    assert rc == 0
    assert cfg.maven.test_excludes == ["FooTest#bar"]


def test_baseline_ask_prompt_abort(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "ask"

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=_red_baseline([FailingTest("com.x.FooTest", "bar")]),
        prompt_fn=lambda msg: "a",
        skip_preflight=True,
    )
    assert rc == 1


def test_baseline_fix_codex_compile_error(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "fix-codex"
    cfg.codex.max_fix_attempts = 2

    def compile_error_baseline(cfg):
        return BaselineResult(
            ok=False, failures=[],
            build=BuildResult(False, 1, None, "package org.joda.time does not exist", "s"),
        )

    builds = _build_seq([
        BuildResult(False, 1, None, "compile error", "s1"),  # preexisting attempt 1
        BuildResult(True, 0, None, "", None),                 # preexisting attempt 2 green
        BuildResult(True, 0, None, "", None),                 # upgrade item build
    ])

    def codex_fix(cfg, tail, *, build_cmd):
        pom.write_text(pom.read_text() + "<!-- fixed joda -->\n")

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=builds,
        codex_fn=lambda *a, **k: None,
        codex_baseline_fn=codex_fix,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=compile_error_baseline,
        skip_preflight=True,
    )
    assert rc == 0
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "fix: resolve pre-existing build failures" in log
    assert "build(deps): bump" in log


def test_baseline_red_no_failing_tests_skip_failing_uses_codex(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.baseline = "skip-failing"
    cfg.codex.max_fix_attempts = 1

    def compile_error_baseline(cfg):
        return BaselineResult(ok=False, failures=[],
                              build=BuildResult(False, 1, None, "compile", "s"))

    rc = run_upgrades(
        cfg, plan_override=([_item()], []),
        build_fn=_build_seq([BuildResult(False, 1, None, "b", "s1")]),
        codex_fn=lambda *a, **k: None,
        codex_baseline_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        baseline_fn=compile_error_baseline,
        skip_preflight=True,
    )
    # codex couldn't fix in 1 attempt -> abort
    assert rc == 1


def test_abort_on_failure_stops(tmp_path):
    _init_repo(tmp_path)
    pom = tmp_path / "pom.xml"
    cfg = _cfg(tmp_path)
    cfg.run.on_failure = "abort"

    item1 = _item("2.0.0")
    item2 = PlanItem(artifact=Artifact(
        group_id="com.example", artifact_id="other", current_version="1.0",
        kind=Kind.DEPENDENCY, version_source=VersionSource.LITERAL,
        declared_in="pom.xml"), target_version="2.0")

    # first item: build always red (1 attempt configured) -> failure -> abort
    cfg.codex.max_fix_attempts = 1
    rc = run_upgrades(
        cfg, plan_override=([item1, item2], []),
        build_fn=_build_seq([BuildResult(False, 1, None, "b", "s1")]),
        codex_fn=lambda *a, **k: None,
        apply_fn=_edit_pom_apply(pom),
        skip_preflight=True,
    )
    assert rc == 0
    import json
    state = json.loads((tmp_path / "dependency-updates.json").read_text())
    gas = [r["artifact_id"] for r in state["results"]]
    assert "lib" in gas and "other" not in gas  # aborted before item2
