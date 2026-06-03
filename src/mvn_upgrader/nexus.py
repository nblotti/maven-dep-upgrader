"""Nexus Repository 3 REST client.

Nexus is the single source of truth for "what versions exist". We never upgrade
to a version that Nexus does not serve, even if Maven Central or the
versions-maven-plugin offers something newer.

Credentials come from env (NEXUS_USER / NEXUS_PASSWORD) and are never logged or
placed in URLs.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from .config import Config
from .models import Candidate

log = logging.getLogger("mvn_upgrader.nexus")

_SEARCH_PATH = "/service/rest/v1/search"


class _Response(Protocol):  # pragma: no cover - structural typing only
    status_code: int

    def json(self) -> dict: ...
    def raise_for_status(self) -> None: ...


class _Session(Protocol):  # pragma: no cover
    def get(self, url: str, *, params: dict, auth, timeout: float) -> _Response: ...


class NexusError(RuntimeError):
    pass


class NexusClient:
    """Lists available versions of a GA across one or more Nexus repositories."""

    def __init__(
        self,
        base_url: str,
        repositories: list[str],
        *,
        auth: Optional[tuple[str, str]] = None,
        extension: str = "jar",
        session: Optional[_Session] = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise NexusError("nexus base_url is required")
        self.base_url = base_url.rstrip("/")
        self.repositories = list(repositories)
        self.auth = auth
        self.extension = extension
        self.timeout = timeout
        self._session = session

    @classmethod
    def from_config(cls, cfg: Config, session: Optional[_Session] = None) -> "NexusClient":
        auth = None
        if cfg.nexus_user and cfg.nexus_password:
            auth = (cfg.nexus_user, cfg.nexus_password)
        return cls(
            cfg.nexus.base_url,
            cfg.nexus.repositories,
            auth=auth,
            extension=cfg.nexus.extension,
            session=session,
        )

    def _session_obj(self):
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _search_repo(
        self, repository: str, group_id: str, artifact_id: str, *, extension: str
    ) -> list[str]:
        """Return all versions of a GA in one repository, following pagination."""
        url = self.base_url + _SEARCH_PATH
        versions: list[str] = []
        seen: set[str] = set()
        token: Optional[str] = None
        session = self._session_obj()
        # Bound pagination to avoid an unterminated loop on a misbehaving server.
        for _ in range(1000):
            params = {
                "repository": repository,
                "maven.groupId": group_id,
                "maven.artifactId": artifact_id,
                "maven.extension": extension,
            }
            if token:
                params["continuationToken"] = token
            resp = session.get(
                url, params=params, auth=self.auth, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("items", []):
                ver = item.get("version") or item.get("maven.baseVersion")
                if ver and ver not in seen:
                    seen.add(ver)
                    versions.append(ver)
            token = data.get("continuationToken")
            if not token:
                break
        return versions

    def list_versions(
        self,
        group_id: str,
        artifact_id: str,
        *,
        extension: Optional[str] = None,
    ) -> list[Candidate]:
        """List candidate versions for a GA, searching repositories in order.

        The first repository that returns any versions wins (matches the
        configured search-order semantics). Returns an empty list if the GA is
        not present in any configured repository.
        """
        ext = extension if extension is not None else self.extension
        for repo in self.repositories:
            try:
                versions = self._search_repo(repo, group_id, artifact_id, extension=ext)
            except Exception as exc:  # network / HTTP errors
                log.warning(
                    "nexus search failed for %s:%s in %s: %s",
                    group_id, artifact_id, repo, type(exc).__name__,
                )
                continue
            if versions:
                return [Candidate(version=v, repository=repo) for v in versions]
        return []
