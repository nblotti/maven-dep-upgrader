"""Maven version ordering + candidate-selection policy.

``ComparableVersion`` is a faithful Python port of Apache Maven's
``org.apache.maven.artifact.versioning.ComparableVersion`` so that "is X newer
than Y?" matches Maven exactly (numeric vs qualifier ranking,
alpha<beta<milestone<rc<release<sp, SNAPSHOT handling, trailing-zero trimming).

Do NOT substitute Python's ``packaging``/PEP 440 here -- the rules differ.
"""

from __future__ import annotations

import re
from fnmatch import fnmatch
from functools import cmp_to_key
from typing import Optional

# ---- Qualifier tables (mirror Maven) -------------------------------------- #
_QUALIFIERS = ["alpha", "beta", "milestone", "rc", "snapshot", "", "sp"]
_RELEASE_INDEX = str(_QUALIFIERS.index(""))  # "5"
_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_SHORT = {"a": "alpha", "b": "beta", "m": "milestone"}


def _strcmp(a: str, b: str) -> int:
    return (a > b) - (a < b)


def _comparable_qualifier(q: str) -> str:
    if q in _QUALIFIERS:
        return str(_QUALIFIERS.index(q))
    return f"{len(_QUALIFIERS)}-{q}"


class _IntItem:
    type = "int"

    def __init__(self, value: int) -> None:
        self.value = value

    def is_null(self) -> bool:
        return self.value == 0

    def compare_to(self, item) -> int:
        if item is None:
            return 0 if self.value == 0 else 1
        if item.type == "int":
            return (self.value > item.value) - (self.value < item.value)
        # int > string, int > list
        return 1


class _StringItem:
    type = "string"

    def __init__(self, value: str, followed_by_digit: bool) -> None:
        if followed_by_digit and len(value) == 1:
            value = _SHORT.get(value, value)
        self.value = _ALIASES.get(value, value)

    def is_null(self) -> bool:
        return _comparable_qualifier(self.value) == _RELEASE_INDEX

    def compare_to(self, item) -> int:
        if item is None:
            return _strcmp(_comparable_qualifier(self.value), _RELEASE_INDEX)
        if item.type == "int":
            return -1
        if item.type == "string":
            return _strcmp(
                _comparable_qualifier(self.value),
                _comparable_qualifier(item.value),
            )
        # string < list
        return -1


class _ListItem:
    type = "list"

    def __init__(self) -> None:
        self.items: list = []

    def add(self, item) -> None:
        self.items.append(item)

    def is_null(self) -> bool:
        return len(self.items) == 0

    def normalize(self) -> None:
        for i in range(len(self.items) - 1, -1, -1):
            last = self.items[i]
            if last.is_null():
                del self.items[i]
            elif last.type != "list":
                break

    def compare_to(self, item) -> int:
        if item is None:
            if not self.items:
                return 0
            return self.items[0].compare_to(None)
        if item.type == "int":
            return -1
        if item.type == "string":
            return 1
        left = self.items
        right = item.items
        n = max(len(left), len(right))
        for i in range(n):
            l = left[i] if i < len(left) else None
            r = right[i] if i < len(right) else None
            if l is None:
                result = 0 if r is None else -1 * r.compare_to(None)
            else:
                result = l.compare_to(r)
            if result != 0:
                return result
        return 0


def _parse_item(is_digit: bool, buf: str):
    if is_digit:
        return _IntItem(int(buf))
    return _StringItem(buf, False)


class ComparableVersion:
    """Compare Maven version strings exactly like Maven does."""

    def __init__(self, version: str) -> None:
        self.value = version
        self.items = self._parse(version)

    @staticmethod
    def _parse(version: str) -> _ListItem:
        version = version.lower()
        items = _ListItem()
        current = items
        stack = [items]
        is_digit = False
        start = 0
        for i, c in enumerate(version):
            if c == ".":
                if i == start:
                    current.add(_IntItem(0))
                else:
                    current.add(_parse_item(is_digit, version[start:i]))
                start = i + 1
            elif c == "-":
                if i == start:
                    current.add(_IntItem(0))
                else:
                    current.add(_parse_item(is_digit, version[start:i]))
                start = i + 1
                new = _ListItem()
                current.add(new)
                current = new
                stack.append(new)
            elif c.isdigit():
                if not is_digit and i > start:
                    current.add(_StringItem(version[start:i], True))
                    start = i
                    new = _ListItem()
                    current.add(new)
                    current = new
                    stack.append(new)
                is_digit = True
            else:
                if is_digit and i > start:
                    current.add(_parse_item(True, version[start:i]))
                    start = i
                    new = _ListItem()
                    current.add(new)
                    current = new
                    stack.append(new)
                is_digit = False
        if len(version) > start:
            current.add(_parse_item(is_digit, version[start:]))
        while stack:
            stack.pop().normalize()
        return items

    def compare_to(self, other: "ComparableVersion") -> int:
        return self.items.compare_to(other.items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ComparableVersion) and self.compare_to(other) == 0

    def __lt__(self, other: "ComparableVersion") -> bool:
        return self.compare_to(other) < 0

    def __hash__(self) -> int:  # pragma: no cover
        return hash(self.value)

    def __repr__(self) -> str:  # pragma: no cover
        return f"ComparableVersion({self.value!r})"


def compare(a: str, b: str) -> int:
    """Return -1/0/1 comparing two Maven versions."""
    return ComparableVersion(a).compare_to(ComparableVersion(b))


# --------------------------------------------------------------------------- #
# Policy filtering
# --------------------------------------------------------------------------- #
_PRERELEASE_RE = re.compile(
    r"(?i)(alpha|beta|rc|cr|m\d|milestone|snapshot|pr|dev|incubat)"
)
_MAJOR_RE = re.compile(r"^\s*(\d+)")


def is_prerelease(version: str) -> bool:
    return bool(_PRERELEASE_RE.search(version))


def major_of(version: str) -> Optional[int]:
    m = _MAJOR_RE.match(version)
    return int(m.group(1)) if m else None


def ga_allowed(ga: str, include: list[str], exclude: list[str]) -> bool:
    """Apply include/exclude globs on ``groupId:artifactId``."""
    if include and not any(fnmatch(ga, pat) for pat in include):
        return False
    if exclude and any(fnmatch(ga, pat) for pat in exclude):
        return False
    return True


def select_target(
    current: str,
    candidates: list[str],
    *,
    allow_major: bool = False,
    exclude_prerelease: bool = True,
    ignore_versions: Optional[list[str]] = None,
    pin: Optional[str] = None,
) -> Optional[str]:
    """Pick the highest allowed candidate strictly newer than ``current``.

    ``pin`` forces a specific version (only if present in ``candidates`` -- Nexus
    remains the source of truth). Returns None if no eligible upgrade exists.
    """
    ignore_res = [re.compile(p) for p in (ignore_versions or [])]
    cur = ComparableVersion(current)
    cur_major = major_of(current)

    if pin is not None:
        if pin in candidates and compare(pin, current) > 0:
            return pin
        return None

    eligible: list[str] = []
    for v in candidates:
        if compare(v, current) <= 0:
            continue
        if exclude_prerelease and is_prerelease(v):
            continue
        if any(r.search(v) for r in ignore_res):
            continue
        if not allow_major and cur_major is not None:
            vm = major_of(v)
            if vm is not None and vm != cur_major:
                continue
        eligible.append(v)

    if not eligible:
        return None
    eligible.sort(key=cmp_to_key(compare))
    return eligible[-1]
