import pytest

from mvn_upgrader.nexus import NexusClient


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Serves paginated search results keyed by repository name."""

    def __init__(self, pages_by_repo):
        self.pages_by_repo = pages_by_repo
        self.requests = []

    def get(self, url, *, params, auth, timeout):
        self.requests.append(dict(params))
        repo = params["repository"]
        pages = self.pages_by_repo.get(repo, [{"items": []}])
        token = params.get("continuationToken")
        idx = 0 if token is None else int(token)
        return FakeResp(pages[idx])


def test_pagination_collects_all_versions():
    pages = {
        "maven-public": [
            {"items": [{"version": "1.0.0"}, {"version": "1.1.0"}],
             "continuationToken": "1"},
            {"items": [{"version": "1.2.0"}]},  # no token -> last page
        ]
    }
    client = NexusClient(
        "https://nexus.example.com", ["maven-public"], session=FakeSession(pages)
    )
    cands = client.list_versions("g", "a")
    assert [c.version for c in cands] == ["1.0.0", "1.1.0", "1.2.0"]
    assert all(c.repository == "maven-public" for c in cands)


def test_first_repo_with_results_wins():
    pages = {
        "repo-empty": [{"items": []}],
        "repo-has": [{"items": [{"version": "2.0.0"}]}],
    }
    client = NexusClient(
        "https://nexus.example.com",
        ["repo-empty", "repo-has"],
        session=FakeSession(pages),
    )
    cands = client.list_versions("g", "a")
    assert [c.version for c in cands] == ["2.0.0"]


def test_not_found_returns_empty():
    client = NexusClient(
        "https://nexus.example.com", ["maven-public"], session=FakeSession({})
    )
    assert client.list_versions("g", "missing") == []


def test_parent_uses_pom_extension():
    pages = {"r": [{"items": [{"version": "3.2.0"}]}]}
    sess = FakeSession(pages)
    client = NexusClient("https://n", ["r"], extension="jar", session=sess)
    client.list_versions("org.springframework.boot", "spring-boot-starter-parent",
                         extension="pom")
    assert sess.requests[0]["maven.extension"] == "pom"


def test_base_version_fallback_field():
    pages = {"r": [{"items": [{"maven.baseVersion": "9.9.9"}]}]}
    client = NexusClient("https://n", ["r"], session=FakeSession(pages))
    assert [c.version for c in client.list_versions("g", "a")] == ["9.9.9"]


def test_no_credentials_in_request_params():
    pages = {"r": [{"items": [{"version": "1.0"}]}]}
    sess = FakeSession(pages)
    client = NexusClient(
        "https://n", ["r"], auth=("user", "secret"), session=sess
    )
    client.list_versions("g", "a")
    for req in sess.requests:
        assert "secret" not in str(req)
