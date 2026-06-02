"""Version ordering tests using Apache Maven's own ComparableVersionTest vectors."""

import pytest

from mvn_upgrader.versioning import (
    ComparableVersion,
    compare,
    ga_allowed,
    is_prerelease,
    major_of,
    select_target,
)

# These two lists are taken verbatim from Maven's ComparableVersionTest and are
# in strictly ascending order.
VERSIONS_QUALIFIER = [
    "1-alpha2snapshot", "1-alpha2", "1-alpha-123", "1-beta-2", "1-beta123",
    "1-m2", "1-m11", "1-rc", "1-cr2", "1-rc123", "1-SNAPSHOT", "1", "1-sp",
    "1-sp2", "1-sp123", "1-abc", "1-def", "1-pom-1", "1-1-snapshot", "1-1",
    "1-2", "1-123",
]

VERSIONS_NUMBER = [
    "2.0", "2-1", "2.0.a", "2.0.0.a", "2.0.2", "2.0.123", "2.1.0", "2.1-a",
    "2.1b", "2.1-c", "2.1-1", "2.1.0.1", "2.2", "2.123", "11.a2", "11.a11",
    "11.b2", "11.b11", "11.m2", "11.m11", "11", "11.a", "11b", "11c", "11m",
]


@pytest.mark.parametrize("seq", [VERSIONS_QUALIFIER, VERSIONS_NUMBER])
def test_strict_ascending(seq):
    for lo, hi in zip(seq, seq[1:]):
        assert compare(lo, hi) < 0, f"{lo} should be < {hi}"
        assert compare(hi, lo) > 0, f"{hi} should be > {lo}"
        assert compare(lo, lo) == 0


@pytest.mark.parametrize(
    "a,b",
    [
        ("1", "1.0"),
        ("1", "1.0.0"),
        ("1.0", "1.0.0"),
        ("1.0-alpha-1", "1.0a1"),
        ("1.0-beta-1", "1.0b1"),
        ("1.0-milestone-1", "1.0m1"),
        ("1.0-cr1", "1.0-rc1"),
        ("1.0-ga", "1.0"),
        ("1.0-final", "1.0"),
        ("1.0-release", "1.0"),
    ],
)
def test_equalities(a, b):
    assert compare(a, b) == 0, f"{a} should equal {b}"


def test_snapshot_and_sp_ordering():
    assert compare("1.0-SNAPSHOT", "1.0") < 0
    assert compare("1.0", "1.0-sp") < 0
    assert compare("1.0-rc1", "1.0-SNAPSHOT") < 0
    assert compare("1.0-alpha-1", "1.0-beta-1") < 0


def test_comparable_version_richcmp():
    assert ComparableVersion("1.2.0") < ComparableVersion("1.10.0")
    assert ComparableVersion("1.0") == ComparableVersion("1.0.0")


def test_is_prerelease():
    assert is_prerelease("1.0.0-SNAPSHOT")
    assert is_prerelease("2.1.0-RC1")
    assert is_prerelease("1.0-m2")
    assert is_prerelease("3.0.0-alpha")
    assert not is_prerelease("3.0.0")
    assert not is_prerelease("1.2.3.RELEASE")


def test_major_of():
    assert major_of("1.2.3") == 1
    assert major_of("10.0") == 10
    assert major_of("notaversion") is None


def test_ga_allowed():
    assert ga_allowed("com.example:lib", [], [])
    assert ga_allowed("com.example:lib", ["com.example:*"], [])
    assert not ga_allowed("org.other:x", ["com.example:*"], [])
    assert not ga_allowed("com.example:lib", [], ["com.example:*"])


CANDS = ["1.2.0", "1.2.1", "1.3.0", "2.0.0", "1.2.0-SNAPSHOT", "1.4.0-beta"]


def test_select_target_default():
    assert select_target("1.2.0", CANDS) == "1.3.0"


def test_select_target_allow_major():
    assert select_target("1.2.0", CANDS, allow_major=True) == "2.0.0"


def test_select_target_ignore_versions():
    assert select_target("1.2.0", CANDS, ignore_versions=[r"1\.3\.0"]) == "1.2.1"


def test_select_target_pin():
    assert select_target("1.2.0", CANDS, pin="1.2.1") == "1.2.1"
    assert select_target("1.2.0", CANDS, pin="9.9.9") is None


def test_select_target_none_when_current_is_latest():
    assert select_target("2.0.0", CANDS, allow_major=True) is None


def test_select_target_keeps_prerelease_when_allowed():
    assert select_target(
        "1.2.0", CANDS, allow_major=True, exclude_prerelease=False
    ) == "2.0.0"
