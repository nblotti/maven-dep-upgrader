"""Core data structures shared across the tool.

These are intentionally plain dataclasses with explicit (de)serialization helpers
so the run state in ``dependency-updates.json`` is stable and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class Kind(str, Enum):
    """What sort of artifact an entry represents."""

    DEPENDENCY = "dependency"
    PLUGIN = "plugin"


class VersionSource(str, Enum):
    """Where the *editable* version of an artifact lives.

    Drives which apply strategy is used (see ``apply.py``).
    """

    LITERAL = "literal"      # hard-coded <version> in a POM
    PROPERTY = "property"    # <version>${prop}</version>
    MANAGED = "managed"      # comes from dependencyManagement / BOM
    PARENT = "parent"        # the artifact *is* the <parent> coordinate
    NONE = "none"            # no editable version (pure transitive / unresolved)


class Status(str, Enum):
    """Final disposition of a plan item in the report."""

    UPGRADED = "upgraded"
    SKIPPED_BUILD_FAILED = "skipped-build-failed"
    SKIPPED_NO_NEWER = "skipped-no-newer"
    NOT_IN_NEXUS = "not-in-nexus"
    MANAGED_EXTERNAL = "managed-external"
    INFORMATIONAL = "informational"
    PENDING = "pending"
    ERROR = "error"


@dataclass(frozen=True)
class Coordinate:
    """A Maven groupId:artifactId pair."""

    group_id: str
    artifact_id: str

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.ga


@dataclass
class Artifact:
    """A discovered dependency or plugin with its editable version source."""

    group_id: str
    artifact_id: str
    current_version: Optional[str]
    kind: Kind
    version_source: VersionSource
    # POM file that actually declares the editable version (may be a parent).
    declared_in: Optional[str] = None
    # For VersionSource.PROPERTY: the property name (without ${ }).
    property_name: Optional[str] = None
    # Module POM paths in which this artifact is used (for reporting).
    used_in: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def coord(self) -> Coordinate:
        return Coordinate(self.group_id, self.artifact_id)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["version_source"] = self.version_source.value
        return d


@dataclass
class Candidate:
    """A single available version of an artifact, as reported by Nexus."""

    version: str
    repository: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanItem:
    """A concrete intended upgrade: one artifact from current -> target."""

    artifact: Artifact
    target_version: str
    # Other GAs that share the same property and will move together (property bumps).
    co_moved: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ga(self) -> str:
        return self.artifact.ga

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact.to_dict(),
            "target_version": self.target_version,
            "co_moved": list(self.co_moved),
            "reason": self.reason,
        }


@dataclass
class UpgradeResult:
    """Outcome of attempting (or planning) a single upgrade."""

    group_id: str
    artifact_id: str
    kind: Kind
    version_source: VersionSource
    old_version: Optional[str]
    new_version: Optional[str]
    status: Status
    commit: Optional[str] = None
    fix_attempts: int = 0
    co_moved: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["version_source"] = self.version_source.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UpgradeResult":
        return cls(
            group_id=d["group_id"],
            artifact_id=d["artifact_id"],
            kind=Kind(d["kind"]),
            version_source=VersionSource(d["version_source"]),
            old_version=d.get("old_version"),
            new_version=d.get("new_version"),
            status=Status(d["status"]),
            commit=d.get("commit"),
            fix_attempts=d.get("fix_attempts", 0),
            co_moved=list(d.get("co_moved", [])),
            notes=list(d.get("notes", [])),
        )

    @classmethod
    def informational(cls, artifact: Artifact, note: str) -> "UpgradeResult":
        return cls(
            group_id=artifact.group_id,
            artifact_id=artifact.artifact_id,
            kind=artifact.kind,
            version_source=artifact.version_source,
            old_version=artifact.current_version,
            new_version=None,
            status=Status.INFORMATIONAL,
            notes=[note],
        )
