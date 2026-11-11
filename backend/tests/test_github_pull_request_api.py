"""通过真实应用、客户端与流式 MockTransport 离线验证 PR 路由。"""

import base64
import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient

import app.main as main_module
from app.github_client import (
    GitHubClientError,
    GitHubFileTooLargeError,
    GitHubForbiddenError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRedirectError,
    GitHubRepositoryClient,
    GitHubTimeoutError,
    GitHubUnsupportedFileError,
    GitHubUpstreamError,
)
from app.main import app, get_github_repository_client
from app.schemas import GitHubPullRequestResponse

API_PATH = "/api/v1/github/pull-request"
REPOSITORY_URL = "https://GitHub.Com/Sample-Owner/Sample-Repo/"
PULL_NUMBER = 42
HTML_URL = "https://github.com/Sample-Owner/Sample-Repo/pull/42"
UPSTREAM_URL = "https://api.github.com/repos/Sample-Owner/Sample-Repo/pulls/42"
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 响应。"
RESPONSE_FIELDS = {
    "repository_url",
    "pull_number",
    "title",
    "state",
    "draft",
    "merged",
    "html_url",
    "base_ref",
    "head_ref",
    "base_sha",
    "head_sha",
    "changed_files",
    "additions",
    "deletions",
}


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """只阻断真实 HTTP 传输和 DNS，不干扰 TestClient 的本地事件循环。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR API 测试禁止真实 HTTP 传输和 DNS 查询")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)


@pytest.fixture(autouse=True)
def restore_dependency_overrides() -> Iterator[None]:
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def pull_payload() -> dict[str, object]:
    return {
        "number": PULL_NUMBER,
        "title": "修复重试 🙂",
        "state": "open",
        "draft": False,
        "merged": False,
        "html_url": HTML_URL,
        "base": {"ref": "发布/版本一", "sha": "a1" * 20},
        "head": {"ref": "feature/重试🚀", "sha": "b2" * 20, "repo": None},
        "changed_files": 2,
        "additions": 10,
        "deletions": 3,
        "body": "RAW_SECRET",
        "diff_url": "https://evil.invalid/RAW_SECRET",
        "patch_url": "http://127.0.0.1/RAW_SECRET",
    }


class TrackingStream(httpx.SyncByteStream):
    def __init__(
        self,
        steps: list[bytes | httpx.RequestError],
        close_error: httpx.RequestError | None = None,
    ) -> None:
        self.steps = steps
        self.consumed: list[bytes | httpx.RequestError] = []
        self.close_count = 0
        self.close_error = close_error

    def __iter__(self) -> Iterator[bytes]:
        for step in self.steps:
            self.consumed.append(step)
            if isinstance(step, httpx.RequestError):
                raise step
            yield step

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


class TrackingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


@dataclass
class MockUpstream:
    payload: dict[str, object] = field(default_factory=pull_payload)
    handler: Callable[[httpx.Request], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)
    streams: list[TrackingStream] = field(default_factory=list)
    clients: list[GitHubRepositoryClient] = field(default_factory=list)
    transports: list[TrackingTransport] = field(default_factory=list)

    def response(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        steps: list[bytes | httpx.RequestError] | None = None,
        close_error: httpx.RequestError | None = None,
    ) -> httpx.Response:
        stream = TrackingStream([body] if steps is None else steps, close_error)
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    def json_response(self, payload: object) -> httpx.Response:
        # 显式序列化，None 是 JSON null，不是 httpx 的空响应体快捷写法。
        return self.response(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = (
            self.handler(request) if self.handler is not None else self.json_response(self.payload)
        )
        self.responses.append(response)
        return response

    def new_client(self) -> GitHubRepositoryClient:
        transport = TrackingTransport(self.respond)
        client = GitHubRepositoryClient(transport=transport)
        self.transports.append(transport)
        self.clients.append(client)
        return client


def assert_resources_closed(upstream: MockUpstream, *, clients: int, responses: int) -> None:
    """先核对对象数量，再逐个检查关闭，避免空集合产生虚假的关闭证据。"""
    assert len(upstream.clients) == len(upstream.transports) == clients
    assert len(upstream.responses) == len(upstream.streams) == responses
    for client in upstream.clients:
        assert client.is_closed
    for transport in upstream.transports:
        assert transport.close_count == 1
    for response in upstream.responses:
        assert response.is_closed
    for stream in upstream.streams:
        assert stream.close_count == 1


@pytest.fixture
def upstream() -> MockUpstream:
    return MockUpstream()


@pytest.fixture
def override_client(upstream: MockUpstream) -> Iterator[None]:
    def dependency() -> Iterator[GitHubRepositoryClient]:
        with upstream.new_client() as client:
            yield client

    app.dependency_overrides[get_github_repository_client] = dependency
    yield


@pytest.fixture
def api_client(override_client: None) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def pr_fetch_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[GitHubRepositoryClient, str, int]]:
    """只在 PR 场景限制其他业务调用，兼容性测试不使用本 fixture。"""
    calls: list[tuple[GitHubRepositoryClient, str, int]] = []
    original_fetch = GitHubRepositoryClient.fetch_pull_request

    def spy(
        self: GitHubRepositoryClient, repository_url: str, pull_number: int
    ) -> GitHubPullRequestResponse:
        calls.append((self, repository_url, pull_number))
        return original_fetch(self, repository_url, pull_number)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 路由不得查询仓库、读取文件或调用源码审查器")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request", spy)
    monkeypatch.setattr(GitHubRepositoryClient, "fetch_repository", forbidden)
    monkeypatch.setattr(GitHubRepositoryClient, "fetch_python_file", forbidden)
    monkeypatch.setattr(main_module, "review_python", forbidden)
    return calls


@pytest.fixture
def pr_api_client(
    api_client: TestClient, pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]]
) -> TestClient:
    return api_client


def request_body() -> dict[str, object]:
    return {"repository_url": REPOSITORY_URL, "pull_number": PULL_NUMBER}


def assert_single_pull_call(
    calls: list[tuple[GitHubRepositoryClient, str, int]], upstream: MockUpstream
) -> None:
    assert len(upstream.clients) == 1
    assert calls == [(upstream.clients[0], REPOSITORY_URL, PULL_NUMBER)]


@pytest.mark.parametrize(
    ("state", "draft", "merged", "counts"),
    [
        ("open", False, False, (2, 10, 3)),
        ("closed", False, False, (2, 10, 3)),
        ("closed", False, True, (2, 10, 3)),
        ("open", True, False, (2, 10, 3)),
        ("open", False, False, (0, 0, 0)),
    ],
    ids=["open", "closed-unmerged", "merged", "draft", "zero-counts"],
)
def test_success_returns_exact_flat_fields_and_one_fixed_request(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
    state: str,
    draft: bool,
    merged: bool,
    counts: tuple[int, int, int],
) -> None:
    upstream.payload.update(
        state=state,
        draft=draft,
        merged=merged,
        changed_files=counts[0],
        additions=counts[1],
        deletions=counts[2],
    )
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json() == {
        "repository_url": REPOSITORY_URL,
        "pull_number": PULL_NUMBER,
        "title": "修复重试 🙂",
        "state": state,
        "draft": draft,
        "merged": merged,
        "html_url": HTML_URL,
        "base_ref": "发布/版本一",
        "head_ref": "feature/重试🚀",
        "base_sha": "a1" * 20,
        "head_sha": "b2" * 20,
        "changed_files": counts[0],
        "additions": counts[1],
        "deletions": counts[2],
    }
    assert set(response.json()) == RESPONSE_FIELDS
    assert "RAW_SECRET" not in response.text
    assert_single_pull_call(pr_fetch_spy, upstream)
    assert len(upstream.requests) == 1
    request = upstream.requests[0]
    assert request.method == "GET"
    assert str(request.url) == UPSTREAM_URL
    assert request.headers["Accept-Encoding"] == "identity"
    assert "Authorization" not in request.headers
    assert_resources_closed(upstream, clients=1, responses=1)


def test_code_like_metadata_is_returned_as_data_without_review_or_execution(
    pr_api_client: TestClient, upstream: MockUpstream, tmp_path: Path
) -> None:
    marker = tmp_path / "metadata-must-not-execute.txt"
    title = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    upstream.payload.update(title=title, base={"ref": "print('not source')", "sha": "a1" * 20})
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json()["title"] == title
    assert response.json()["base_ref"] == "print('not source')"
    assert "review" not in response.json()
    assert not marker.exists()
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize(
    ("body", "location"),
    [
        ({}, ["body", "repository_url"]),
        ({"repository_url": REPOSITORY_URL}, ["body", "pull_number"]),
        ({"pull_number": PULL_NUMBER}, ["body", "repository_url"]),
        ({"repository_url": 123, "pull_number": PULL_NUMBER}, ["body", "repository_url"]),
        ({"repository_url": True, "pull_number": PULL_NUMBER}, ["body", "repository_url"]),
        ({"repository_url": None, "pull_number": PULL_NUMBER}, ["body", "repository_url"]),
        (
            {"repository_url": "http://github.com/a/b", "pull_number": 42},
            ["body", "repository_url"],
        ),
        (
            {"repository_url": "https://github.com@evil.invalid/a/b", "pull_number": 42},
            ["body", "repository_url"],
        ),
        (
            {"repository_url": "https://user:RAW_SECRET@github.com/a/b", "pull_number": 42},
            ["body", "repository_url"],
        ),
        ({"repository_url": REPOSITORY_URL, "pull_number": True}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": False}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": "RAW_SECRET"}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": 42.0}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": None}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": 0}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": -1}, ["body", "pull_number"]),
        ({"repository_url": REPOSITORY_URL, "pull_number": 2_147_483_648}, ["body", "pull_number"]),
        ({**request_body(), "token": "RAW_SECRET"}, ["body", "token"]),
        ({**request_body(), "api_url": "https://evil.invalid/RAW_SECRET"}, ["body", "api_url"]),
        (None, ["body"]),
        (["RAW_SECRET"], ["body"]),
        ("RAW_SECRET", ["body"]),
        (123, ["body"]),
        (True, ["body"]),
    ],
)
def test_invalid_input_keeps_safe_422_format_without_querying_client(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
    body: object,
    location: list[str],
) -> None:
    response = pr_api_client.post(
        API_PATH, content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    details = response.json()["detail"]
    assert isinstance(details, list) and details
    assert all(set(issue) == {"loc", "type", "msg"} for issue in details)
    assert any(issue["loc"] == location for issue in details)
    assert "RAW_SECRET" not in response.text
    assert pr_fetch_spy == []
    assert upstream.requests == []
    # 框架可能先构造依赖。确认实际数量后，只检查已创建的客户端确实关闭。
    created = len(upstream.clients)
    assert created in {0, 1}
    assert_resources_closed(upstream, clients=created, responses=0)


def test_malformed_json_is_rejected_before_fetch_and_hides_raw_input(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
) -> None:
    body = '{"repository_url":"RAW_SECRET", "pull_number":'
    with pytest.raises(json.JSONDecodeError) as json_error:
        json.loads(body)
    response = pr_api_client.post(
        API_PATH, content=body, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    details = response.json()["detail"]
    assert isinstance(details, list) and details
    assert all(set(issue) == {"loc", "type", "msg"} for issue in details)
    assert details[0]["loc"] == ["body", json_error.value.pos]
    assert details[0]["type"] == "json_invalid"
    assert "RAW_SECRET" not in response.text
    assert pr_fetch_spy == []
    assert upstream.requests == []
    created = len(upstream.clients)
    assert created in {0, 1}
    assert_resources_closed(upstream, clients=created, responses=0)


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected_status", "detail", "read_body"),
    [
        (404, {}, b"RAW_SECRET", 404, "仓库或 PR 不存在或不可公开访问。", False),
        (429, {}, b"RAW_SECRET", 503, "GitHub 请求受到限流，请稍后重试。", False),
        (
            403,
            {"X-RateLimit-Remaining": " 0 "},
            b"RAW_SECRET",
            503,
            "GitHub 请求受到限流，请稍后重试。",
            False,
        ),
        (
            403,
            {},
            b'{"message":"secondary rate limit RAW_SECRET"}',
            503,
            "GitHub 请求受到限流，请稍后重试。",
            True,
        ),
        (403, {}, b'{"message":"RAW_SECRET forbidden"}', 502, "GitHub 拒绝访问该仓库或 PR。", True),
        (
            302,
            {"Location": "http://127.0.0.1/RAW_SECRET"},
            b"",
            502,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
            False,
        ),
        (500, {}, b"RAW_SECRET", 502, UPSTREAM_MESSAGE, False),
        (204, {}, b"", 502, UPSTREAM_MESSAGE, False),
        (200, {}, b"{RAW_SECRET invalid", 502, UPSTREAM_MESSAGE, True),
        (200, {}, b"null", 502, UPSTREAM_MESSAGE, True),
        (200, {}, b"\xff", 502, UPSTREAM_MESSAGE, True),
        (200, {"Content-Encoding": "gzip"}, b"RAW_SECRET", 502, UPSTREAM_MESSAGE, False),
    ],
)
def test_real_client_errors_have_correct_fixed_http_mapping(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
    status: int,
    headers: dict[str, str],
    body: bytes,
    expected_status: int,
    detail: str,
    read_body: bool,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return upstream.response(
            body,
            status=status,
            headers=headers,
            steps=None if read_body else [httpx.ReadError("RAW_SECRET must not be read")],
        )

    upstream.handler = handler
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert_single_pull_call(pr_fetch_spy, upstream)
    assert len(upstream.requests) == 1
    assert str(upstream.requests[0].url) == UPSTREAM_URL
    if not read_body:
        assert upstream.streams[0].consumed == []
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"number": PULL_NUMBER + 1},
        {"html_url": "https://evil.invalid/RAW_SECRET"},
        {"title": None},
        {"base": None},
        {"head": {"ref": "feature", "sha": "A" * 40}},
    ],
)
def test_invalid_response_or_identity_mismatch_is_502_and_releases_resources(
    pr_api_client: TestClient, upstream: MockUpstream, changes: dict[str, object]
) -> None:
    upstream.payload.update(changes)
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == 502
    assert response.json() == {"detail": UPSTREAM_MESSAGE}
    assert "RAW_SECRET" not in response.text
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("status", [200, 403])
def test_pr_body_overflow_is_502_and_stops_reading(
    pr_api_client: TestClient, upstream: MockUpstream, status: int
) -> None:
    oversized = b"x" * 1_048_577

    def handler(_request: httpx.Request) -> httpx.Response:
        return upstream.response(
            b"",
            status=status,
            steps=[oversized, httpx.ReadError("RAW_SECRET must not be consumed")],
        )

    upstream.handler = handler
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == 502
    assert response.json() == {"detail": UPSTREAM_MESSAGE}
    assert len(upstream.requests) == 1
    assert upstream.streams[0].consumed == [oversized]
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize(
    ("error_type", "expected_status", "detail"),
    [
        (httpx.ConnectTimeout, 504, "GitHub 请求超时。"),
        (httpx.ConnectError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_connection_failure_has_no_response_but_closes_created_client(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    error_type: type[httpx.RequestError],
    expected_status: int,
    detail: str,
) -> None:
    def handler(request: httpx.Request) -> NoReturn:
        raise error_type("RAW_SECRET connect details", request=request)

    upstream.handler = handler
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize(
    ("error_type", "expected_status", "detail"),
    [
        (httpx.ReadTimeout, 504, "GitHub 请求超时。"),
        (httpx.ReadError, 502, "GitHub 网络请求失败。"),
    ],
)
@pytest.mark.parametrize("phase", ["read", "exit"])
def test_read_or_close_failure_releases_response_and_client(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    error_type: type[httpx.RequestError],
    expected_status: int,
    detail: str,
    phase: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        error = error_type("RAW_SECRET stream details", request=request)
        if phase == "read":
            return upstream.response(b"", steps=[b'{"number":', error, b"unread"])
        return upstream.response(b"", status=404, close_error=error)

    upstream.handler = handler
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert len(upstream.requests) == 1
    assert len(upstream.streams[0].consumed) == (2 if phase == "read" else 0)
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize(
    ("error_type", "expected_status", "detail"),
    [
        (GitHubNotFoundError, 404, "仓库或 PR 不存在或不可公开访问。"),
        (GitHubRateLimitError, 503, "GitHub 请求受到限流，请稍后重试。"),
        (GitHubForbiddenError, 502, "GitHub 拒绝访问该仓库或 PR。"),
        (GitHubRedirectError, 502, "GitHub 返回了重定向，请使用仓库当前的主页地址。"),
        (GitHubUpstreamError, 502, UPSTREAM_MESSAGE),
        (GitHubTimeoutError, 504, "GitHub 请求超时。"),
        (GitHubNetworkError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_seven_client_exceptions_never_expose_their_original_text(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[GitHubClientError],
    expected_status: int,
    detail: str,
) -> None:
    def fail_fetch(self: GitHubRepositoryClient, repository_url: str, pull_number: int) -> NoReturn:
        pr_fetch_spy.append((self, repository_url, pull_number))
        raise error_type("RAW_SECRET upstream body, headers and internal details")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request", fail_fetch)
    response = pr_api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert_single_pull_call(pr_fetch_spy, upstream)
    assert upstream.requests == []
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize(
    "error_type",
    [
        ValueError,
        RuntimeError,
        GitHubClientError,
        GitHubFileTooLargeError,
        GitHubUnsupportedFileError,
    ],
)
def test_unexpected_exceptions_are_not_converted_to_business_responses(
    pr_api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    original_error = error_type("RAW_SECRET unexpected programming error")

    def fail_fetch(self: GitHubRepositoryClient, repository_url: str, pull_number: int) -> NoReturn:
        raise original_error

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request", fail_fetch)
    with pytest.raises(error_type) as error:
        pr_api_client.post(API_PATH, json=request_body())
    assert error.value is original_error
    assert upstream.requests == []
    assert_resources_closed(upstream, clients=1, responses=0)


def test_response_validation_is_not_wrapped_as_a_github_query_error(
    pr_api_client: TestClient, upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_result(
        self: GitHubRepositoryClient, repository_url: str, pull_number: int
    ) -> dict[str, object]:
        return {"title": "RAW_SECRET"}

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request", invalid_result)
    with pytest.raises(ResponseValidationError) as error:
        pr_api_client.post(API_PATH, json=request_body())
    errors = error.value.errors()
    assert errors
    assert any(
        issue["loc"] == ("response", "repository_url") and issue["type"] == "missing"
        for issue in errors
    )
    assert upstream.requests == []
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize("scenario", ["success", "not-found", "invalid-response"])
def test_production_dependency_creates_and_closes_distinct_clients(
    upstream: MockUpstream,
    pr_fetch_spy: list[tuple[GitHubRepositoryClient, str, int]],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    # 保留真实 Depends -> with/yield，只替换构造器以注入离线传输。
    monkeypatch.setattr(main_module, "GitHubRepositoryClient", upstream.new_client)
    assert app.dependency_overrides == {}
    expected_status = 200
    if scenario == "not-found":
        upstream.handler = lambda _: upstream.response(b"RAW_SECRET", status=404)
        expected_status = 404
    elif scenario == "invalid-response":
        upstream.handler = lambda _: upstream.json_response(None)
        expected_status = 502

    with TestClient(app) as client:
        for request_number in range(1, 3):
            response = client.post(API_PATH, json=request_body())
            assert response.status_code == expected_status
            if scenario == "success":
                assert response.json()["pull_number"] == PULL_NUMBER
            elif scenario == "not-found":
                assert response.json() == {"detail": "仓库或 PR 不存在或不可公开访问。"}
            else:
                assert response.json() == {"detail": UPSTREAM_MESSAGE}
            assert_resources_closed(upstream, clients=request_number, responses=request_number)
            assert len(pr_fetch_spy) == len(upstream.requests) == request_number
    assert upstream.clients[0] is not upstream.clients[1]
    assert app.dependency_overrides == {}


def test_four_existing_business_routes_remain_compatible(
    api_client: TestClient, upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_pr(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("旧路由不应调用 PR 查询")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request", forbidden_pr)
    health = api_client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    review = api_client.post("/api/v1/reviews", json={"code": "x = 1\n"})
    assert review.status_code == 200
    assert review.json() == {"language": "python", "issue_count": 0, "issues": []}
    empty = api_client.post("/api/v1/reviews", json={"code": ""})
    assert empty.status_code == 422
    assert empty.json()["detail"][0]["loc"] == ["body", "code"]
    assert_resources_closed(upstream, clients=0, responses=0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/Sample-Owner/Sample-Repo":
            return upstream.json_response(
                {
                    "full_name": "Sample-Owner/Sample-Repo",
                    "description": None,
                    "language": "Python",
                    "stargazers_count": 7,
                    "default_branch": "main",
                    "archived": False,
                    "html_url": REPOSITORY_URL,
                }
            )
        if request.url.path == "/repos/Sample-Owner/Sample-Repo/contents/src/api.py":
            source = b"print('compatibility')\n"
            return upstream.json_response(
                {
                    "type": "file",
                    "encoding": "base64",
                    "size": len(source),
                    "path": "src/api.py",
                    "name": "api.py",
                    "content": base64.b64encode(source).decode("ascii"),
                }
            )
        pytest.fail(f"意外的旧路由请求：{request.url.path}")

    upstream.handler = handler
    repository = api_client.post(
        "/api/v1/github/repository", json={"repository_url": REPOSITORY_URL}
    )
    assert repository.status_code == 200
    assert repository.json() == {
        "full_name": "Sample-Owner/Sample-Repo",
        "description": None,
        "language": "Python",
        "stars": 7,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
    }
    file_review = api_client.post(
        "/api/v1/github/file-review",
        json={"repository_url": REPOSITORY_URL, "file_path": "src/api.py"},
    )
    assert file_review.status_code == 200
    file_result = file_review.json()
    assert file_result["repository_url"] == REPOSITORY_URL
    assert file_result["file_path"] == "src/api.py"
    assert file_result["review"]["language"] == "python"
    assert file_result["review"]["issue_count"] == 1
    assert file_result["review"]["issues"][0]["rule_id"] == "PY_PRINT"
    assert len(upstream.requests) == 2
    assert_resources_closed(upstream, clients=2, responses=2)


def test_openapi_exposes_five_routes_and_the_new_model_contract() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    expected_methods = {
        "/health": {"get"},
        "/api/v1/reviews": {"post"},
        "/api/v1/github/repository": {"post"},
        "/api/v1/github/file-review": {"post"},
        API_PATH: {"post"},
    }
    assert set(document["paths"]) == set(expected_methods)
    for path, methods in expected_methods.items():
        assert set(document["paths"][path]) == methods
    operation = document["paths"][API_PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/GitHubPullRequestRequest"
    )
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/GitHubPullRequestResponse"
    )
    schemas = document["components"]["schemas"]
    request_schema = schemas["GitHubPullRequestRequest"]
    assert set(request_schema["properties"]) == {"repository_url", "pull_number"}
    assert set(request_schema["required"]) == {"repository_url", "pull_number"}
    assert request_schema["additionalProperties"] is False
    response_schema = schemas["GitHubPullRequestResponse"]
    assert set(response_schema["properties"]) == RESPONSE_FIELDS
    assert set(response_schema["required"]) == RESPONSE_FIELDS
    assert response_schema["additionalProperties"] is False
    assert response_schema["properties"]["state"]["enum"] == ["open", "closed"]
    assert get_github_repository_client not in app.dependency_overrides
