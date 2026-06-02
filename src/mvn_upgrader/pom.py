"""POM discovery & ``version_source`` classification.

The hard part of the tool. We obtain *effective* versions (parent + BOM +
property resolution) via Maven's ``help:effective-pom`` and figure out *where*
each version is editable by inspecting the raw POMs in the repo.

The Maven-invoking parts are thin and mockable; the parsing/classification is
pure so it can be unit-tested against fixture POMs without a Maven install. When
Maven is unavailable, ``discover`` falls back to resolving an approximate
effective model from the raw POMs alone.
"""

from __future__ import annotations

import logging
import re
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import proc
from .config import Config
from .models import Artifact, Kind, VersionSource

log = logging.getLogger("mvn_upgrader.pom")

_PROP_RE = re.compile(r"\$\{([^}]+)\}")


# --------------------------------------------------------------------------- #
# XML helpers (namespace-agnostic)
# --------------------------------------------------------------------------- #
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, name: str) -> Optional[ET.Element]:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _text(el: Optional[ET.Element], name: str) -> Optional[str]:
    if el is None:
        return None
    child = _find(el, name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _strip_ns(text: str) -> str:
    # Remove the default xmlns so ElementTree tags are clean local names.
    return re.sub(r'\sxmlns="[^"]+"', "", text, count=1)


# --------------------------------------------------------------------------- #
# Parsed representations
# --------------------------------------------------------------------------- #
@dataclass
class Dep:
    group_id: str
    artifact_id: str
    version: Optional[str]  # raw text (may be ${prop} or literal); None if absent

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


@dataclass
class RawPom:
    path: str
    group_id: Optional[str]
    artifact_id: Optional[str]
    version: Optional[str]
    parent: Optional[Dep]
    properties: dict[str, str] = field(default_factory=dict)
    dependencies: list[Dep] = field(default_factory=list)
    managed: list[Dep] = field(default_factory=list)
    plugins: list[Dep] = field(default_factory=list)
    plugin_managed: list[Dep] = field(default_factory=list)
    module_dirs: list[str] = field(default_factory=list)

    @property
    def ga(self) -> Optional[str]:
        gid = self.group_id or (self.parent.group_id if self.parent else None)
        if gid and self.artifact_id:
            return f"{gid}:{self.artifact_id}"
        return None


@dataclass
class EffectiveModule:
    group_id: str
    artifact_id: str
    version: str
    parent: Optional[Dep]
    dependencies: list[Dep] = field(default_factory=list)
    plugins: list[Dep] = field(default_factory=list)
    path: Optional[str] = None  # raw pom path, when known

    @property
    def ga(self) -> str:
        return f"{self.group_id}:{self.artifact_id}"


# --------------------------------------------------------------------------- #
# Raw POM parsing
# --------------------------------------------------------------------------- #
def _parse_deps(container: Optional[ET.Element]) -> list[Dep]:
    out: list[Dep] = []
    if container is None:
        return out
    for dep in container:
        if _local(dep.tag) not in ("dependency", "plugin"):
            continue
        gid = _text(dep, "groupId")
        aid = _text(dep, "artifactId")
        ver = _text(dep, "version")
        if aid:
            out.append(Dep(gid or "", aid, ver))
    return out


def parse_raw_pom(path: str, text: str) -> RawPom:
    root = ET.fromstring(_strip_ns(text))
    parent_el = _find(root, "parent")
    parent = None
    if parent_el is not None:
        parent = Dep(
            _text(parent_el, "groupId") or "",
            _text(parent_el, "artifactId") or "",
            _text(parent_el, "version"),
        )

    props: dict[str, str] = {}
    props_el = _find(root, "properties")
    if props_el is not None:
        for p in props_el:
            props[_local(p.tag)] = (p.text or "").strip()

    deps = _parse_deps(_find(root, "dependencies"))
    dm = _find(root, "dependencyManagement")
    managed = _parse_deps(_find(dm, "dependencies")) if dm is not None else []

    build = _find(root, "build")
    plugins = _parse_deps(_find(build, "plugins")) if build is not None else []
    pm_el = _find(build, "pluginManagement") if build is not None else None
    plugin_managed = _parse_deps(_find(pm_el, "plugins")) if pm_el is not None else []

    modules_el = _find(root, "modules")
    module_dirs = []
    if modules_el is not None:
        for m in modules_el:
            if _local(m.tag) == "module" and m.text:
                module_dirs.append(m.text.strip())

    return RawPom(
        path=path,
        group_id=_text(root, "groupId"),
        artifact_id=_text(root, "artifactId"),
        version=_text(root, "version"),
        parent=parent,
        properties=props,
        dependencies=deps,
        managed=managed,
        plugins=plugins,
        plugin_managed=plugin_managed,
        module_dirs=module_dirs,
    )


def discover_module_poms(repo: Path) -> list[str]:
    """Walk ``<modules>`` from the root POM to enumerate every reactor POM path."""
    root_pom = repo / "pom.xml"
    if not root_pom.is_file():
        return []
    found: list[str] = []
    seen: set[str] = set()

    def visit(pom_path: Path) -> None:
        rp = str(pom_path.resolve())
        if rp in seen or not pom_path.is_file():
            return
        seen.add(rp)
        found.append(rp)
        try:
            raw = parse_raw_pom(rp, pom_path.read_text(encoding="utf-8"))
        except ET.ParseError:
            log.warning("could not parse %s", rp)
            return
        for mod in raw.module_dirs:
            child = (pom_path.parent / mod)
            child_pom = child / "pom.xml" if child.is_dir() else child
            visit(child_pom)

    visit(root_pom)
    return found


@dataclass
class RawIndex:
    poms: dict[str, RawPom]  # path -> RawPom
    by_coord: dict[str, str]  # ga -> path (for in-repo modules)

    def parent_chain(self, path: str) -> list[RawPom]:
        """Return [pom, parent, grandparent, ...] limited to in-repo POMs."""
        chain: list[RawPom] = []
        seen: set[str] = set()
        cur = self.poms.get(path)
        while cur is not None and cur.path not in seen:
            seen.add(cur.path)
            chain.append(cur)
            if cur.parent is None:
                break
            pp = self.by_coord.get(cur.parent.ga)
            cur = self.poms.get(pp) if pp else None
        return chain

    def resolve_property(self, name: str, chain: list[RawPom]) -> Optional[str]:
        for pom in chain:
            if name in pom.properties:
                val = pom.properties[name]
                m = _PROP_RE.fullmatch(val.strip())
                if m:  # property points at another property
                    return self.resolve_property(m.group(1), chain)
                return val
        return None


def build_raw_index(repo: Path) -> RawIndex:
    poms: dict[str, RawPom] = {}
    by_coord: dict[str, str] = {}
    for path in discover_module_poms(repo):
        try:
            rp = parse_raw_pom(path, Path(path).read_text(encoding="utf-8"))
        except ET.ParseError:
            continue
        poms[path] = rp
        if rp.ga:
            by_coord[rp.ga] = path
    return RawIndex(poms=poms, by_coord=by_coord)


# --------------------------------------------------------------------------- #
# Effective POM parsing (Maven output) and raw fallback
# --------------------------------------------------------------------------- #
def parse_effective_poms(text: str) -> list[EffectiveModule]:
    """Parse ``help:effective-pom`` output.

    Handles both a single ``<project>`` document and the reactor ``<projects>``
    wrapper that Maven emits for multi-module builds. XML comments that Maven
    interleaves between projects are tolerated.
    """
    cleaned = _strip_ns(text)
    root = ET.fromstring(cleaned)
    projects: list[ET.Element]
    if _local(root.tag) == "projects":
        projects = [c for c in root if _local(c.tag) == "project"]
    elif _local(root.tag) == "project":
        projects = [root]
    else:
        projects = [c for c in root.iter() if _local(c.tag) == "project"]

    modules: list[EffectiveModule] = []
    for proj in projects:
        gid = _text(proj, "groupId")
        aid = _text(proj, "artifactId")
        ver = _text(proj, "version")
        parent_el = _find(proj, "parent")
        parent = None
        if parent_el is not None:
            parent = Dep(
                _text(parent_el, "groupId") or "",
                _text(parent_el, "artifactId") or "",
                _text(parent_el, "version"),
            )
            if gid is None:
                gid = parent.group_id
            if ver is None:
                ver = parent.version
        if not (gid and aid and ver):
            continue
        deps = _parse_deps(_find(proj, "dependencies"))
        build = _find(proj, "build")
        plugins = _parse_deps(_find(build, "plugins")) if build is not None else []
        modules.append(
            EffectiveModule(gid, aid, ver, parent, deps, plugins)
        )
    return modules


def resolve_effective_from_raw(index: RawIndex) -> list[EffectiveModule]:
    """Approximate effective modules using only the raw POMs in the repo.

    Used when Maven is unavailable. Resolves properties and in-repo
    dependencyManagement; cannot see external parent/BOM-managed versions
    (those are left as None and later classified as managed-external).
    """
    modules: list[EffectiveModule] = []
    for path, rp in index.poms.items():
        chain = index.parent_chain(path)

        # Build a managed-version map from the parent chain.
        managed_map: dict[str, Optional[str]] = {}
        for pom in chain:
            for d in pom.managed:
                managed_map.setdefault(d.ga, d.version)
        plugin_managed_map: dict[str, Optional[str]] = {}
        for pom in chain:
            for d in pom.plugin_managed:
                plugin_managed_map.setdefault(d.ga, d.version)

        def resolve(raw_ver: Optional[str]) -> Optional[str]:
            if raw_ver is None:
                return None
            m = _PROP_RE.fullmatch(raw_ver.strip())
            if m:
                return index.resolve_property(m.group(1), chain)
            return raw_ver

        eff_deps: list[Dep] = []
        for d in rp.dependencies:
            ver = d.version or managed_map.get(d.ga)
            eff_deps.append(Dep(d.group_id, d.artifact_id, resolve(ver)))
        eff_plugins: list[Dep] = []
        for d in rp.plugins:
            ver = d.version or plugin_managed_map.get(d.ga)
            eff_plugins.append(Dep(d.group_id, d.artifact_id, resolve(ver)))

        gid = rp.group_id or (rp.parent.group_id if rp.parent else None)
        ver = rp.version or (rp.parent.version if rp.parent else None)
        if not (gid and rp.artifact_id):
            continue
        modules.append(
            EffectiveModule(
                gid, rp.artifact_id, ver or "0", rp.parent, eff_deps, eff_plugins, path
            )
        )
    return modules


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@dataclass
class Declaration:
    version_source: VersionSource
    declared_in: Optional[str]
    property_name: Optional[str] = None


def _classify_one(
    ga: str,
    raw_version_holder: str,
    section: str,  # "deps" or "plugins"
    module_path: Optional[str],
    index: RawIndex,
) -> Declaration:
    """Find where a GA's version is editable by walking the in-repo parent chain."""
    chain = index.parent_chain(module_path) if module_path else list(index.poms.values())

    def scan(pom: RawPom) -> Optional[tuple[Optional[str], str]]:
        # returns (raw_version_text, where) where in {"plain","managed"}
        if section == "deps":
            for d in pom.dependencies:
                if d.ga == ga and d.version is not None:
                    return d.version, "plain"
            for d in pom.managed:
                if d.ga == ga and d.version is not None:
                    return d.version, "managed"
        else:
            for d in pom.plugins:
                if d.ga == ga and d.version is not None:
                    return d.version, "plain"
            for d in pom.plugin_managed:
                if d.ga == ga and d.version is not None:
                    return d.version, "managed"
        return None

    for pom in chain:
        hit = scan(pom)
        if hit is None:
            continue
        raw_ver, where = hit
        m = _PROP_RE.fullmatch((raw_ver or "").strip())
        if m:
            return Declaration(VersionSource.PROPERTY, pom.path, m.group(1))
        if where == "managed":
            return Declaration(VersionSource.MANAGED, pom.path)
        return Declaration(VersionSource.LITERAL, pom.path)

    # Not declared anywhere in-repo -> managed by an external parent/BOM.
    return Declaration(VersionSource.MANAGED, None)


def classify_artifacts(
    modules: list[EffectiveModule], index: RawIndex
) -> list[Artifact]:
    """Produce a deduplicated list of editable Artifacts from effective modules."""
    by_key: dict[tuple[str, str], Artifact] = {}
    in_repo_coords = set(index.by_coord)

    def add(art: Artifact) -> None:
        key = (art.kind.value, art.ga)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = art
        else:
            for u in art.used_in:
                if u not in existing.used_in:
                    existing.used_in.append(u)

    for mod in modules:
        # Dependencies
        for d in mod.dependencies:
            if d.ga in in_repo_coords:
                continue  # internal reactor module, not an upgrade target
            decl = _classify_one(d.ga, d.ga, "deps", mod.path, index)
            add(
                Artifact(
                    group_id=d.group_id,
                    artifact_id=d.artifact_id,
                    current_version=d.version,
                    kind=Kind.DEPENDENCY,
                    version_source=decl.version_source,
                    declared_in=decl.declared_in,
                    property_name=decl.property_name,
                    used_in=[mod.path] if mod.path else [],
                )
            )
        # Plugins
        for d in mod.plugins:
            decl = _classify_one(d.ga, d.ga, "plugins", mod.path, index)
            add(
                Artifact(
                    group_id=d.group_id,
                    artifact_id=d.artifact_id,
                    current_version=d.version,
                    kind=Kind.PLUGIN,
                    version_source=decl.version_source,
                    declared_in=decl.declared_in,
                    property_name=decl.property_name,
                    used_in=[mod.path] if mod.path else [],
                )
            )
        # Parent coordinate (upgradeable via versions:update-parent).
        if mod.parent and mod.parent.ga not in in_repo_coords and mod.parent.version:
            add(
                Artifact(
                    group_id=mod.parent.group_id,
                    artifact_id=mod.parent.artifact_id,
                    current_version=mod.parent.version,
                    kind=Kind.DEPENDENCY,
                    version_source=VersionSource.PARENT,
                    declared_in=mod.path,
                    used_in=[mod.path] if mod.path else [],
                )
            )

    return list(by_key.values())


# --------------------------------------------------------------------------- #
# Maven invocation + top-level discovery
# --------------------------------------------------------------------------- #
def _mvn_base(cfg: Config) -> list[str]:
    cmd = [cfg.maven.mvn_executable, "-B", "-ntp"]
    if cfg.maven.settings:
        cmd += ["-s", cfg.maven.settings]
    return cmd


def run_effective_pom(cfg: Config, runner=proc.run) -> Optional[str]:
    """Run ``help:effective-pom`` from the repo root; return XML text or None."""
    repo = cfg.repo
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "effective.xml"
        cmd = _mvn_base(cfg) + ["help:effective-pom", f"-Doutput={out}"]
        res = runner(cmd, cwd=repo)
        if not res.ok or not out.is_file():
            log.warning("help:effective-pom failed (rc=%s)", res.returncode)
            return None
        return out.read_text(encoding="utf-8")


@dataclass
class DiscoveryResult:
    artifacts: list[Artifact]
    modules: list[EffectiveModule]
    used_fallback: bool


def discover(cfg: Config, runner=proc.run) -> DiscoveryResult:
    """Discover all editable dependency/plugin artifacts in the repo."""
    repo = cfg.repo
    index = build_raw_index(repo)

    modules: list[EffectiveModule] = []
    used_fallback = True
    if proc.which(cfg.maven.mvn_executable):
        xml = run_effective_pom(cfg, runner=runner)
        if xml:
            try:
                modules = parse_effective_poms(xml)
                used_fallback = False
                # Attach raw pom paths to effective modules where possible.
                for mod in modules:
                    p = index.by_coord.get(mod.ga)
                    if p:
                        mod.path = p
            except ET.ParseError:
                log.warning("could not parse effective-pom output")

    if not modules:
        log.info("falling back to raw-POM resolution (no usable effective-pom)")
        modules = resolve_effective_from_raw(index)
        used_fallback = True

    artifacts = classify_artifacts(modules, index)
    artifacts.sort(key=lambda a: (a.kind.value, a.ga))
    return DiscoveryResult(artifacts=artifacts, modules=modules, used_fallback=used_fallback)
