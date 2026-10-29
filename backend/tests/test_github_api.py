"""使用 TestClient、依赖覆盖和真实客户端加 MockTransport，离线验证新路由。"""

import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import NoReturn

import httpx
import pytest
from fastapi.testclient import TestClient

from app.github_client import (
    GitHubClientError,
    GitHubForbiddenError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRedirectError,
    GitHubRepositoryClient,
    GitHubTimeoutError,
    GitHubUpstreamError,
)
from app.main import app, get_github_repository_client
from app.schemas import GitHubRepositoryResponse

API_PATH = "/api/v1/github/repository"
REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断真实 HTTP 传输和 DNS，同时允许 TestClient 的内部事件循环工作。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("API 集成测试禁止真实 HTTP 请求和 DNS 查询")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)


@pytest.fixture(autouse=True)
def restore_dependency_overrides() -> Iterator[None]:
    """每项测试结束后恢复原有覆盖，即使断言失败也不污染后续测试。"""
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.fixture
def repository_payload() -> dict[str, object]:
    """固定模拟数据，不代表 GitHub 的实时统计。"""
    return {
        "full_name": "sample-owner/sample-repo",
        "description": "示例仓库",
        "language": "Python",
        "stargazers_count": 12,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
        "id": 123,
        "owner": {"login": "sample-owner"},
        "token": "RAW_SECRET",
    }


@dataclass
class MockUpstream:
    """保存可控响应处理器、实际发出的请求及创建过的客户端。"""

    handler: Callable[[httpx.Request], httpx.Response]
    requests: list[httpx.Request] = field(default_factory=list)
    clients: list[GitHubRepositoryClient] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def upstream(repository_payload: dict[str, object]) -> MockUpstream:
    return MockUpstream(lambda _: httpx.Response(200, json=repository_payload))


@pytest.fixture
def api_client(upstream: MockUpstream) -> Iterator[TestClient]:
    def override_client() -> Iterator[GitHubRepositoryClient]:
        with GitHubRepositoryClient(transport=httpx.MockTransport(upstream.respond)) as client:
            upstream.clients.append(client)
            yield client

    app.dependency_overrides[get_github_repository_client] = override_client
    with TestClient(app) as client:
        yield client
    assert all(client.is_closed for client in upstream.clients)


def test_success_returns_only_the_seven_expected_fields(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    response = api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL})

    assert response.status_code == 200
    assert response.json() == {
        "full_name": "sample-owner/sample-repo",
        "description": "示例仓库",
        "language": "Python",
        "stars": 12,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
    }
    assert "RAW_SECRET" not in response.text
    assert len(upstream.requests) == 1
    assert upstream.requests[0].method == "GET"
    assert str(upstream.requests[0].url) == (
        "https://api.github.com/repos/sample-owner/sample-repo"
    )
    assert "Authorization" not in upstream.requests[0].headers
    assert len(upstream.clients) == 1
    assert upstream.clients[0].is_closed


@pytest.mark.parametrize(
    ("description", "language"), [(None, None), (None, "Python"), ("示例仓库", None)]
)
def test_null_fields_survive_api_serialization(
    api_client: TestClient,
    repository_payload: dict[str, object],
    description: str | None,
    language: str | None,
) -> None:
    repository_payload.update(description=description, language=language)
    response = api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL})

    assert response.status_code == 200
    assert response.json()["description"] == description
    assert response.json()["language"] == language
    assert response.json()["stars"] == 12
    assert len(response.json()) == 7


@pytest.mark.parametrize(
    ("body", "location", "error_type"),
    [
        (None, ["body"], "missing"),
        ({}, ["body", "repository_url"], "missing"),
        ({"repository_url": 123}, ["body", "repository_url"], "string_type"),
        ({"repository_url": True}, ["body", "repository_url"], "string_type"),
        ({"repository_url": None}, ["body", "repository_url"], "string_type"),
        ({"repository_url": []}, ["body", "repository_url"], "string_type"),
        ({"repository_url": {}}, ["body", "repository_url"], "string_type"),
        ({"repository_url": ""}, ["body", "repository_url"], "string_too_short"),
        ({"repository_url": "x" * 2049}, ["body", "repository_url"], "string_too_long"),
        (
            {"repository_url": REPOSITORY_URL, "token": "RAW_SECRET"},
            ["body", "token"],
            "extra_forbidden",
        ),
        (
            {"repository_url": REPOSITORY_URL, "api_url": "https://evil.invalid"},
            ["body", "api_url"],
            "extra_forbidden",
        ),
    ],
)
def test_invalid_fields_keep_the_existing_422_format(
    api_client: TestClient,
    upstream: MockUpstream,
    body: object,
    location: list[str],
    error_type: str,
) -> None:
    response = api_client.post(API_PATH, json=body)

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert any(error["loc"] == location and error["type"] == error_type for error in errors)
    assert "RAW_SECRET" not in response.text
    assert upstream.requests == []
    # FastAPI 可能先创建依赖；关键是没有请求上游，且已创建的资源被释放。
    assert all(client.is_closed for client in upstream.clients)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://evil.invalid/owner/repo",
        "https://github.com.evil.invalid/owner/repo",
        "https://github.com@evil.invalid/owner/repo",
        "https://user:RAW_SECRET@github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/repo?api_url=https://evil.invalid",
        "https://github.com/owner/repo#fragment",
        "https://github.com/owner/repo/tree/main",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo%2f..%2fprivate",
        "\nhttps://github.com/owner/repo",
    ],
)
def test_invalid_urls_return_422_without_http_requests(
    api_client: TestClient, upstream: MockUpstream, url: str
) -> None:
    response = api_client.post(API_PATH, json={"repository_url": url})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "repository_url"]
    assert all(set(error) == {"loc", "type", "msg"} for error in response.json()["detail"])
    assert "RAW_SECRET" not in response.text
    assert upstream.requests == []


@pytest.mark.parametrize("body", ["", '{"repository_url":', r'{"repository_url":"\ud800"}'])
def test_missing_malformed_or_invalid_unicode_json_is_rejected(
    api_client: TestClient, upstream: MockUpstream, body: str
) -> None:
    response = api_client.post(API_PATH, content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("upstream_status", "headers", "body", "expected_status", "detail"),
    [
        (404, {}, "RAW_SECRET", 404, "仓库不存在或不可公开访问。"),
        (429, {}, "RAW_SECRET", 503, "GitHub 请求受到限流，请稍后重试。"),
        (
            403,
            {"X-RateLimit-Remaining": "0"},
            "RAW_SECRET",
            503,
            "GitHub 请求受到限流，请稍后重试。",
        ),
        (
            403,
            {},
            '{"message":"API rate limit exceeded RAW_SECRET"}',
            503,
            "GitHub 请求受到限流，请稍后重试。",
        ),
        (403, {}, "RAW_SECRET", 502, "GitHub 拒绝访问该仓库。"),
        (
            301,
            {"Location": "https://evil.invalid/RAW_SECRET"},
            "RAW_SECRET",
            502,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
        ),
        (500, {}, "RAW_SECRET", 502, "GitHub 返回了无效或非预期的响应。"),
        (200, {}, "RAW_SECRET invalid JSON", 502, "GitHub 返回了无效或非预期的响应。"),
        (200, {}, "[]", 502, "GitHub 返回了无效或非预期的响应。"),
        (200, {}, "{}", 502, "GitHub 返回了无效或非预期的响应。"),
    ],
)
def test_upstream_responses_are_mapped_to_safe_http_errors(
    api_client: TestClient,
    upstream: MockUpstream,
    upstream_status: int,
    headers: dict[str, str],
    body: str,
    expected_status: int,
    detail: str,
) -> None:
    upstream.handler = lambda _: httpx.Response(upstream_status, headers=headers, text=body)
    response = api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL})

    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert REPOSITORY_URL not in response.text
    assert len(upstream.requests) == 1
    assert upstream.requests[0].url.host == "api.github.com"
    assert len(upstream.clients) == 1
    assert upstream.clients[0].is_closed


@pytest.mark.parametrize(
    ("error_type", "status", "detail"),
    [
        (httpx.ReadTimeout, 504, "GitHub 请求超时。"),
        (httpx.ConnectError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_transport_errors_are_mapped_to_http_errors(
    api_client: TestClient,
    upstream: MockUpstream,
    error_type: type[httpx.RequestError],
    status: int,
    detail: str,
) -> None:
    def fail_request(request: httpx.Request) -> httpx.Response:
        raise error_type("RAW_SECRET network details", request=request)

    upstream.handler = fail_request
    response = api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL})

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert len(upstream.requests) == 1
    assert upstream.clients[0].is_closed


@pytest.mark.parametrize(
    ("error_type", "status", "detail"),
    [
        (GitHubNotFoundError, 404, "仓库不存在或不可公开访问。"),
        (GitHubRateLimitError, 503, "GitHub 请求受到限流，请稍后重试。"),
        (GitHubForbiddenError, 502, "GitHub 拒绝访问该仓库。"),
        (GitHubRedirectError, 502, "GitHub 返回了重定向，请使用仓库当前的主页地址。"),
        (GitHubUpstreamError, 502, "GitHub 返回了无效或非预期的响应。"),
        (GitHubTimeoutError, 504, "GitHub 请求超时。"),
        (GitHubNetworkError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_route_never_returns_the_client_exception_text(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[GitHubClientError],
    status: int,
    detail: str,
) -> None:
    """直接注入含敏感文本的客户端异常，防止路由依赖客户端已做过脱敏。"""

    def fail_fetch(self: GitHubRepositoryClient, repository_url: str) -> GitHubRepositoryResponse:
        raise error_type(f"RAW_SECRET upstream JSON and headers: {repository_url}")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_repository", fail_fetch)
    response = api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL})

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert REPOSITORY_URL not in response.text
    assert upstream.requests == []
    assert upstream.clients[0].is_closed


def test_existing_endpoints_do_not_use_the_github_dependency(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    health = api_client.get("/health")
    review = api_client.post("/api/v1/reviews", json={"code": "total = 1 + 2"})
    invalid_review = api_client.post("/api/v1/reviews", json={"code": ""})

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert review.status_code == 200
    assert review.json() == {"language": "python", "issue_count": 0, "issues": []}
    assert invalid_review.status_code == 422
    assert all(set(error) == {"loc", "type", "msg"} for error in invalid_review.json()["detail"])
    assert upstream.requests == []
    assert upstream.clients == []


@pytest.mark.parametrize("scenario", ["success", "upstream-error", "invalid-input"])
def test_production_dependency_closes_a_separate_client_per_request(
    monkeypatch: pytest.MonkeyPatch, upstream: MockUpstream, scenario: str
) -> None:
    """不覆盖 Depends，只替换构造器的传输层，真正执行生产依赖的 yield/with。"""

    def create_mock_client() -> GitHubRepositoryClient:
        client = GitHubRepositoryClient(transport=httpx.MockTransport(upstream.respond))
        upstream.clients.append(client)
        return client

    monkeypatch.setattr("app.main.GitHubRepositoryClient", create_mock_client)
    body = {"repository_url": REPOSITORY_URL}
    expected_status = 200
    if scenario == "upstream-error":
        upstream.handler = lambda _: httpx.Response(404)
        expected_status = 404
    elif scenario == "invalid-input":
        body = {"repository_url": "https://evil.invalid/owner/repo"}
        expected_status = 422

    assert get_github_repository_client not in app.dependency_overrides
    with TestClient(app) as client:
        response = client.post(API_PATH, json=body)
        assert response.status_code == expected_status
        assert all(created.is_closed for created in upstream.clients)
        if scenario == "success":
            second_response = client.post(API_PATH, json=body)
            assert second_response.status_code == 200
            assert len(upstream.clients) == 2
            assert upstream.clients[0] is not upstream.clients[1]
        elif scenario == "upstream-error":
            assert len(upstream.clients) == 1
        else:
            assert upstream.requests == []
    assert all(created.is_closed for created in upstream.clients)
