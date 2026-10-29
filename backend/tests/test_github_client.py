"""使用 MockTransport 离线验证 GitHub 客户端，不连接 GitHub 或查询 DNS。"""

import json
import socket
from collections.abc import Callable
from typing import NoReturn

import httpx
import pytest

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
from app.schemas import GitHubRepositoryResponse

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
UPSTREAM_ERROR_MESSAGE = "GitHub 返回了无效或非预期的响应。"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """即使漏用 MockTransport，测试也不能退回真实网络。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("客户端单元测试禁止真实 DNS 查询和网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def repository_payload() -> dict[str, object]:
    """模拟 GitHub 数据，不代表实时仓库信息。"""
    return {
        "full_name": "sample-owner/sample-repo",
        "description": "示例仓库",
        "language": "Python",
        "stargazers_count": 12,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
    }


def test_success_returns_only_validated_fields(repository_payload: dict[str, object]) -> None:
    repository_payload.update(
        id=123, owner={"login": "sample-owner"}, stars=999, token="RAW_SECRET"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=repository_payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_repository(REPOSITORY_URL)

    assert isinstance(result, GitHubRepositoryResponse)
    assert result.model_dump() == {
        "full_name": "sample-owner/sample-repo",
        "description": "示例仓库",
        "language": "Python",
        "stars": 12,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
    }


@pytest.mark.parametrize(
    ("description", "language"), [(None, None), (None, "Python"), ("示例仓库", None)]
)
def test_nullable_fields_are_preserved(
    repository_payload: dict[str, object], description: str | None, language: str | None
) -> None:
    repository_payload.update(description=description, language=language)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=repository_payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_repository(REPOSITORY_URL)
    assert result.description == description
    assert result.language == language


@pytest.mark.parametrize(
    ("url", "path"),
    [
        (REPOSITORY_URL, "/repos/sample-owner/sample-repo"),
        ("https://GitHub.Com/PsF/ReQuEsTs/", "/repos/PsF/ReQuEsTs"),
        ("https://github.com/Example-Owner/Repo_1.2", "/repos/Example-Owner/Repo_1.2"),
    ],
)
def test_request_target_headers_and_timeouts(
    repository_payload: dict[str, object], url: str, path: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=repository_payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        client.fetch_repository(url)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"https://api.github.com{path}"
    assert request.url.host == "api.github.com"
    assert request.url.path == path
    assert request.headers["Host"] == "api.github.com"
    assert request.headers["User-Agent"] == "RepoPilot"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
    assert "Authorization" not in request.headers
    assert request.extensions["timeout"] == {
        "connect": 3.0,
        "read": 5.0,
        "write": 5.0,
        "pool": 5.0,
    }


def test_httpx_keeps_tls_verification_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockTransport 不进行 TLS 握手，因此单独核实传给 HTTPX 的 TLS 配置。"""
    settings: dict[str, object] = {}
    original_client = httpx.Client

    def capture_settings(**kwargs: object) -> httpx.Client:
        settings.update(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr("app.github_client.httpx.Client", capture_settings)
    with GitHubRepositoryClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))):
        pass
    assert settings["verify"] is True
    assert settings["follow_redirects"] is False
    assert settings["base_url"] == "https://api.github.com"
    assert settings["trust_env"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.invalid/owner/repo",
        "https://github.com.evil.invalid/owner/repo",
        "https://github.com@evil.invalid/owner/repo",
        "http://github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/repo%2f..%2fprivate",
        "https://github.com/owner/repo?api_url=https://evil.invalid",
        "https://github.com/owner/repo/tree/main",
        "https://127.0.0.1/owner/repo",
        "https://github.com/owner/repo.git",
        "\nhttps://github.com/owner/repo",
    ],
)
def test_invalid_url_is_rejected_before_transport(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        pytest.fail("无效 URL 不得进入 HTTP 传输层")

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            client.fetch_repository(url)
    assert requests == []


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (404, GitHubNotFoundError, "仓库不存在或不可公开访问。"),
        (429, GitHubRateLimitError, "GitHub 请求受到限流，请稍后重试。"),
        (403, GitHubForbiddenError, "GitHub 拒绝访问该仓库。"),
        (201, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (204, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (400, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (401, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (422, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (500, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
        (503, GitHubUpstreamError, UPSTREAM_ERROR_MESSAGE),
    ],
)
def test_status_errors_have_safe_messages(
    status: int, error_type: type[GitHubClientError], message: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="RAW_SECRET upstream body")

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error_type) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == message
    assert "RAW_SECRET" not in str(error.value)
    assert REPOSITORY_URL not in str(error.value)


@pytest.mark.parametrize(
    ("headers", "payload"),
    [
        ({"X-RateLimit-Remaining": "0"}, None),
        ({"X-RateLimit-Remaining": " 0 "}, {"message": "Forbidden"}),
        ({}, {"message": "API rate limit exceeded for this IP"}),
        ({}, {"message": "You have exceeded a secondary rate limit."}),
        ({"X-RateLimit-Remaining": "8"}, {"message": "API RATE LIMIT exceeded"}),
    ],
)
def test_403_requires_explicit_rate_limit_evidence(
    headers: dict[str, str], payload: object
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers=headers, json=payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == "GitHub 请求受到限流，请稍后重试。"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"message": 123},
        {"message": None},
        {"message": "Forbidden"},
        {"message": "denied"},
    ],
)
def test_ordinary_403_is_not_rate_limited(payload: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "X-RateLimit-Remaining": "8",
                "Retry-After": "60",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload),
        )

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubForbiddenError):
            client.fetch_repository(REPOSITORY_URL)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 305, 307, 308, 399])
@pytest.mark.parametrize(
    "location", ["https://evil.invalid/private", "https://api.github.com/repositories/123"]
)
def test_redirects_are_never_followed(status: int, location: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": location}, text="RAW_SECRET")

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubRedirectError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert [str(request.url) for request in requests] == [
        "https://api.github.com/repos/sample-owner/sample-repo"
    ]
    assert location not in str(error.value)
    assert "RAW_SECRET" not in str(error.value)


@pytest.mark.parametrize(
    ("httpx_error", "client_error", "message"),
    [
        (httpx.ConnectTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.WriteTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.PoolTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ConnectError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.ReadError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RemoteProtocolError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RequestError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_transport_errors_are_classified_without_details(
    httpx_error: type[httpx.RequestError], client_error: type[GitHubClientError], message: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx_error("RAW_SECRET low-level network details", request=request)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(client_error) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert len(requests) == 1
    assert str(error.value) == message
    assert "RAW_SECRET" not in str(error.value)


@pytest.mark.parametrize("body", [b"RAW_SECRET not JSON", b"{invalid", b"\xff"])
def test_success_with_invalid_json_is_an_upstream_error(body: bytes) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == UPSTREAM_ERROR_MESSAGE


@pytest.mark.parametrize("payload", [None, [], "RAW_SECRET", 123, True])
def test_success_json_must_be_an_object(payload: object) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == UPSTREAM_ERROR_MESSAGE


@pytest.mark.parametrize(
    "field",
    [
        "full_name",
        "description",
        "language",
        "stargazers_count",
        "default_branch",
        "archived",
        "html_url",
    ],
)
def test_success_requires_every_upstream_field(
    repository_payload: dict[str, object], field: str
) -> None:
    repository_payload.pop(field)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=repository_payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == UPSTREAM_ERROR_MESSAGE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_name", 123),
        ("description", 123),
        ("language", False),
        ("stargazers_count", "12"),
        ("stargazers_count", 12.0),
        ("stargazers_count", True),
        ("stargazers_count", None),
        ("stargazers_count", -1),
        ("default_branch", 123),
        ("archived", "false"),
        ("archived", 0),
        ("html_url", 123),
    ],
)
def test_invalid_field_values_are_safe_upstream_errors(
    repository_payload: dict[str, object], field: str, value: object
) -> None:
    repository_payload[field] = value
    repository_payload["token"] = "RAW_SECRET"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=repository_payload)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert str(error.value) == UPSTREAM_ERROR_MESSAGE
    assert "RAW_SECRET" not in str(error.value)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("json_error", [ValueError, RecursionError])
def test_json_parser_failures_do_not_leak_details(
    monkeypatch: pytest.MonkeyPatch, status: int, json_error: type[Exception]
) -> None:
    response = httpx.Response(status)

    def fail_json() -> NoReturn:
        raise json_error("RAW_SECRET JSON parser details")

    monkeypatch.setattr(response, "json", fail_json)
    transport = httpx.MockTransport(lambda _: response)
    expected_error = GitHubUpstreamError if status == 200 else GitHubForbiddenError
    with GitHubRepositoryClient(transport=transport) as client:
        with pytest.raises(expected_error) as error:
            client.fetch_repository(REPOSITORY_URL)
    assert "RAW_SECRET" not in str(error.value)


class TrackingTransport(httpx.MockTransport):
    """通过公开状态记录传输层是否真正收到了 close 调用。"""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


def test_close_releases_transport_and_prevents_further_requests(
    repository_payload: dict[str, object],
) -> None:
    responses: list[httpx.Response] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, json=repository_payload)
        responses.append(response)
        return response

    transport = TrackingTransport(handler)
    client = GitHubRepositoryClient(transport=transport)
    try:
        assert not client.is_closed
        client.fetch_repository(REPOSITORY_URL)
        assert responses[0].is_closed
    finally:
        client.close()
    assert client.is_closed
    assert transport.close_count == 1
    client.close()
    assert transport.close_count == 1
    with pytest.raises(RuntimeError):
        client.fetch_repository(REPOSITORY_URL)
    assert len(responses) == 1


@pytest.mark.parametrize("status", [200, 404])
def test_context_manager_closes_transport_even_on_errors(
    repository_payload: dict[str, object], status: int
) -> None:
    transport = TrackingTransport(lambda _: httpx.Response(status, json=repository_payload))
    client = GitHubRepositoryClient(transport=transport)
    if status == 200:
        with client as entered:
            assert entered is client
            assert client.fetch_repository(REPOSITORY_URL).stars == 12
    else:
        with pytest.raises(GitHubNotFoundError):
            with client:
                client.fetch_repository(REPOSITORY_URL)
    assert client.is_closed
    assert transport.close_count == 1
