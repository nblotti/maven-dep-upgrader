from pathlib import Path

import pytest

from mvn_upgrader.baseline import (
    BaselineResult,
    FailingTest,
    collect_failures,
    parse_failures_from_log,
    parse_failures_from_xml,
    parse_surefire_failures,
    run_baseline,
    to_test_excludes,
)
from mvn_upgrader.build import BuildResult
from mvn_upgrader.config import Config


SUREFIRE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="com.example.FooTest" tests="3" failures="1" errors="1">
  <testcase name="passes" classname="com.example.FooTest" time="0.1"/>
  <testcase name="failsHere" classname="com.example.FooTest" time="0.2">
    <failure message="expected true">AssertionError</failure>
  </testcase>
  <testcase name="errorsHere" classname="com.example.FooTest" time="0.0">
    <error message="boom">NullPointerException</error>
  </testcase>
</testsuite>
"""

PARAM_XML = """<?xml version="1.0"?>
<testsuite name="com.example.ParamTest">
  <testcase name="paramCase[1]" classname="com.example.ParamTest">
    <failure>x</failure>
  </testcase>
  <testcase name="paramCase[2]" classname="com.example.ParamTest">
    <failure>y</failure>
  </testcase>
</testsuite>
"""

# Surefire 3.x style report with XML namespace on the root element.
NAMESPACED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite xmlns="https://maven.apache.org/surefire/maven-surefire-plugin/xsd/surefire-test-report.xsd"
           name="com.azqore.qoreservice.rbac.QoreserviceRbacUserRolesPermissionsExtractionServiceApplicationTests"
           tests="1" errors="1" failures="0">
  <testcase name="contextLoads"
            classname="com.azqore.qoreservice.rbac.QoreserviceRbacUserRolesPermissionsExtractionServiceApplicationTests"
            time="0.5">
    <error message="Failed to load ApplicationContext" type="java.lang.IllegalStateException"/>
  </testcase>
</testsuite>
"""

LOG_SNIPPET = """
[ERROR] Tests run: 1, Failures: 0, Errors: 1, Skipped: 0
[ERROR] Failed to execute goal ... maven-surefire-plugin:3.2.5:test ...
[ERROR]
[ERROR] Please refer to .../target/surefire-reports
[ERROR]
[ERROR] See ... for the individual test results.
[ERROR] QoreserviceRbacUserRolesPermissionsExtractionServiceApplicationTests.contextLoads » IllegalState Failed to load ApplicationContext
[INFO] BUILD FAILURE
"""


def test_parse_failures_basic():
    fails = parse_failures_from_xml(SUREFIRE_XML)
    sels = sorted(f.selector for f in fails)
    assert sels == ["FooTest#errorsHere", "FooTest#failsHere"]
    # passing test is not included
    assert all("passes" not in f.selector for f in fails)


def test_parse_strips_parametrized_suffix():
    fails = parse_failures_from_xml(PARAM_XML)
    sels = {f.selector for f in fails}
    # both parametrizations collapse to the same method selector
    assert sels == {"ParamTest#paramCase"}


def test_parse_namespaced_surefire_xml():
    fails = parse_failures_from_xml(NAMESPACED_XML)
    assert len(fails) == 1
    assert fails[0].selector == (
        "QoreserviceRbacUserRolesPermissionsExtractionServiceApplicationTests#contextLoads"
    )


def test_parse_failures_from_log():
    fails = parse_failures_from_log(LOG_SNIPPET)
    assert len(fails) == 1
    assert fails[0].selector == (
        "QoreserviceRbacUserRolesPermissionsExtractionServiceApplicationTests#contextLoads"
    )


def test_collect_failures_falls_back_to_log(tmp_path):
    log = tmp_path / "baseline.log"
    log.write_text(LOG_SNIPPET)
    build = BuildResult(False, 1, str(log), LOG_SNIPPET, "sig")
    fails = collect_failures(tmp_path, build)
    assert len(fails) == 1
    assert "contextLoads" in fails[0].selector


def test_parse_bad_xml_returns_empty():
    assert parse_failures_from_xml("<not valid") == []


def test_to_test_excludes_sorted_unique():
    fails = [
        FailingTest("com.b.B", "m2"),
        FailingTest("com.a.A", None),
        FailingTest("com.b.B", "m2"),  # dup
    ]
    assert to_test_excludes(fails) == ["A", "B#m2"]


def test_parse_surefire_failures_walks_reports(tmp_path):
    rpt = tmp_path / "module" / "target" / "surefire-reports"
    rpt.mkdir(parents=True)
    (rpt / "TEST-com.example.FooTest.xml").write_text(SUREFIRE_XML)

    fs = tmp_path / "module" / "target" / "failsafe-reports"
    fs.mkdir(parents=True)
    (fs / "TEST-com.example.ParamTest.xml").write_text(PARAM_XML)

    fails = parse_surefire_failures(tmp_path)
    sels = sorted(f.selector for f in fails)
    assert sels == ["FooTest#errorsHere", "FooTest#failsHere", "ParamTest#paramCase"]


def test_run_baseline_green_skips_parse(tmp_path):
    cfg = Config(repo_path=str(tmp_path))

    def build_fn(cfg, *, attempt_tag, runner=None):
        return BuildResult(True, 0, None, "", None)

    def parse_fn(repo):
        raise AssertionError("should not parse when green")

    res = run_baseline(cfg, build_fn=build_fn, parse_fn=parse_fn)
    assert res.ok and res.failures == []


def test_run_baseline_red_parses(tmp_path):
    cfg = Config(repo_path=str(tmp_path))

    def build_fn(cfg, *, attempt_tag, runner=None):
        return BuildResult(False, 1, None, "tail", "sig")

    def parse_fn(repo, build):
        return [FailingTest("com.example.FooTest", "bar")]

    res = run_baseline(cfg, build_fn=build_fn, parse_fn=parse_fn)
    assert res.ok is False
    assert res.has_failing_tests
    assert res.failures[0].selector == "FooTest#bar"
