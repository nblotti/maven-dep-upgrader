"""Pre-upgrade baseline build + failing-test discovery.

Before any dependency is bumped we run the configured build once to learn the
*current* test status. If tests already fail, the orchestrator can either stop
so they get fixed first, or proceed while excluding those pre-existing failures
from later builds (so the Codex loop only has to fix breakages caused by the
upgrade itself).

Failing tests are parsed from Surefire/Failsafe JUnit XML reports (with a
fallback to Maven's console summary) so we can turn them into
``-Dtest=!Class#method`` exclusion selectors.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import build as build_mod
from .config import Config

log = logging.getLogger("mvn_upgrader.baseline")

# Trailing parametrized-test suffix, e.g. "myTest[1]" / "myTest(int)[2]".
_PARAM_SUFFIX = re.compile(r"[\[(].*$")

_REPORT_GLOBS = (
    "**/target/surefire-reports/TEST-*.xml",
    "**/target/failsafe-reports/TEST-*.xml",
)

# Maven console summary lines, e.g.:
#   [ERROR]   FooTest.bar » IllegalState ...
#   [ERROR]   com.example.FooTest.bar  Time elapsed: 0.1 s  <<< ERROR!
_LOG_ERROR_ARROW = re.compile(
    r"^\[ERROR\]\s+"
    r"(?:[\w.$]+\s+::\s*)?"               # optional "Failures: ..." prefix line
    r"([\w.$]+)\.(\w+)\s*(?:»|Time elapsed:)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class FailingTest:
    classname: str            # fully-qualified or simple class name
    method: Optional[str]     # method name (parametrization suffix stripped)

    @property
    def simple_class(self) -> str:
        return self.classname.rsplit(".", 1)[-1]

    @property
    def selector(self) -> str:
        """A Surefire ``-Dtest`` selector (simple class, optional method)."""
        if self.method:
            return f"{self.simple_class}#{self.method}"
        return self.simple_class


@dataclass
class BaselineResult:
    ok: bool
    failures: list[FailingTest] = field(default_factory=list)
    build: Optional[build_mod.BuildResult] = None

    @property
    def has_failing_tests(self) -> bool:
        return bool(self.failures)


def _local(tag: str) -> str:
    """Strip XML namespace prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1]


def _iter_testcases(root: ET.Element):
    # Surefire may declare a default xmlns; match by local name.
    for el in root.iter():
        if _local(el.tag) == "testcase":
            yield el


def parse_failures_from_xml(text: str) -> list[FailingTest]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: list[FailingTest] = []
    for tc in _iter_testcases(root):
        failed = any(
            _local(child.tag) in ("failure", "error") for child in list(tc)
        )
        if not failed:
            continue
        classname = tc.get("classname") or root.get("name") or ""
        method = tc.get("name") or None
        if method:
            method = _PARAM_SUFFIX.sub("", method).strip() or None
        if classname:
            out.append(FailingTest(classname=classname, method=method))
    return out


def parse_failures_from_log(text: str) -> list[FailingTest]:
    """Parse failing tests from Maven's console output (fallback)."""
    seen: set[tuple[str, Optional[str]]] = set()
    out: list[FailingTest] = []
    for m in _LOG_ERROR_ARROW.finditer(text or ""):
        classname, method = m.group(1), m.group(2)
        key = (classname, method)
        if key not in seen:
            seen.add(key)
            out.append(FailingTest(classname=classname, method=method))
    return out


def parse_surefire_failures(repo: Path) -> list[FailingTest]:
    """Collect failing tests from all Surefire/Failsafe reports under ``repo``."""
    seen: set[tuple[str, Optional[str]]] = set()
    failures: list[FailingTest] = []
    for pattern in _REPORT_GLOBS:
        for xml_path in sorted(repo.glob(pattern)):
            try:
                text = xml_path.read_text(encoding="utf-8")
            except OSError:
                continue
            for ft in parse_failures_from_xml(text):
                key = (ft.classname, ft.method)
                if key not in seen:
                    seen.add(key)
                    failures.append(ft)
    return failures


def _read_build_log(build: Optional[build_mod.BuildResult]) -> str:
    if build is None:
        return ""
    if build.log_path:
        try:
            return Path(build.log_path).read_text(encoding="utf-8")
        except OSError:
            pass
    return build.tail or ""


def collect_failures(
    repo: Path, build: Optional[build_mod.BuildResult]
) -> list[FailingTest]:
    """Discover failing tests from Surefire XML, then Maven console output."""
    failures = parse_surefire_failures(repo)
    if failures:
        return failures
    log_text = _read_build_log(build)
    if log_text:
        failures = parse_failures_from_log(log_text)
        if failures:
            log.info(
                "parsed %d failing test(s) from build log (no XML reports found)",
                len(failures),
            )
    return failures


def to_test_excludes(failures: list[FailingTest]) -> list[str]:
    """Stable, de-duplicated list of ``-Dtest`` selectors for failing tests."""
    selectors: list[str] = []
    seen: set[str] = set()
    for ft in failures:
        sel = ft.selector
        if sel not in seen:
            seen.add(sel)
            selectors.append(sel)
    return sorted(selectors)


def run_baseline(
    cfg: Config,
    *,
    build_fn: Callable = build_mod.run,
    parse_fn: Callable[[Path, Optional[build_mod.BuildResult]], list[FailingTest]]
    | None = None,
) -> BaselineResult:
    """Run the baseline build and, if red, parse the pre-existing failing tests."""
    if parse_fn is None:
        parse_fn = lambda repo, build: collect_failures(repo, build)

    res = build_fn(cfg, attempt_tag="baseline")
    if res.ok:
        return BaselineResult(ok=True, failures=[], build=res)
    failures = parse_fn(cfg.repo, res)
    return BaselineResult(ok=False, failures=failures, build=res)
