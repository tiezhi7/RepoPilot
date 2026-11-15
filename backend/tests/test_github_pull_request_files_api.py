"""真实应用和客户端配合流式 MockTransport，离线验证 PR 文件列表路由。"""

import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
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
from app.main import app
from app.schemas import GitHubPullRequestFilesResponse

API_PATH = "/api/v1/github/pull-request/files"
REPOSITORY_URL = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件列表响应。"
ERROR_CASES = [
    (GitHubNotFoundError, 404, "仓库或 PR 不存在或不可公开访问。"),
    (GitHubRateLimitError, 503, "GitHub 请求受到限流，请稍后重试。"),
    (GitHubForbiddenError, 502, "GitHub 拒绝访问该仓库或 PR。"),
    (GitHubRedirectError, 502, "GitHub 返回了重定向，请使用仓库当前的主页地址。"),
    (GitHubUpstreamError, 502, UPSTREAM_MESSAGE),
    (GitHubTimeoutError, 504, "GitHub 请求超时。"),
    (GitHubNetworkError, 502, "GitHub 网络请求失败。"),
]


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """保留 TestClient 的本地事件循环，只阻断真实 HTTP 传输和 DNS。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 文件列表 API 测试禁止真实 HTTP 传输和 DNS 查询")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail_network)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
        monkeypatch.setattr(socket, name, fail_network)


@pytest.fixture(autouse=True)
def restore_dependency_overrides() -> Iterator[None]:
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


def file_payload() -> dict[str, object]:
    return {
        "filename": "文档/新 文件.md",
        "status": "renamed",
        "additions": 4,
        "deletions": 2,
        "changes": 6,
        "previous_filename": "文档/旧 文件.md",
        "patch": "RAW_SECRET",
        "raw_url": "http://127.0.0.1/private",
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
    payload: object = field(default_factory=lambda: [file_payload()])
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    steps: list[bytes | httpx.RequestError] | None = None
    close_error: httpx.RequestError | None = None
    connection_error: httpx.RequestError | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)
    streams: list[TrackingStream] = field(default_factory=list)
    clients: list[GitHubRepositoryClient] = field(default_factory=list)
    transports: list[TrackingTransport] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.connection_error is not None:
            raise self.connection_error
        body = json.dumps(self.payload, ensure_ascii=True).encode("utf-8")
        stream = TrackingStream([body] if self.steps is None else self.steps, self.close_error)
        response = httpx.Response(self.status, headers=self.headers, stream=stream)
        self.streams.append(stream)
        self.responses.append(response)
        return response

    def new_client(self) -> GitHubRepositoryClient:
        transport = TrackingTransport(self.respond)
        client = GitHubRepositoryClient(transport=transport)
        self.transports.append(transport)
        self.clients.append(client)
        return client


def assert_resources_closed(upstream: MockUpstream, *, clients: int, responses: int) -> None:
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
def upstream(monkeypatch: pytest.MonkeyPatch) -> MockUpstream:
    value = MockUpstream()
    # 只替换构造器以注入 MockTransport，真实生产依赖的 with/yield 仍然执行。
    monkeypatch.setattr(main_module, "GitHubRepositoryClient", value.new_client)
    return value


@pytest.fixture
def fetch_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    calls: list[tuple[str, int, int]] = []
    original = GitHubRepositoryClient.fetch_pull_request_files

    def spy(
        self: GitHubRepositoryClient, repository_url: str, pull_number: int, page: int = 1
    ) -> GitHubPullRequestFilesResponse:
        calls.append((repository_url, pull_number, page))
        return original(self, repository_url, pull_number, page)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("文件列表路由不得查询其他接口、下载文件或调用源码审查器")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_files", spy)
    for name in ("fetch_repository", "fetch_python_file", "fetch_pull_request"):
        monkeypatch.setattr(GitHubRepositoryClient, name, forbidden)
    monkeypatch.setattr(main_module, "review_python", forbidden)
    return calls


@pytest.fixture
def api_client(
    upstream: MockUpstream, fetch_calls: list[tuple[str, int, int]]
) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def request_body(**changes: object) -> dict[str, object]:
    return {"repository_url": REPOSITORY_URL, "pull_number": 42, **changes}


@pytest.mark.parametrize("page", [None, 2, 100])
def test_success_returns_exact_contract_and_one_request(
    api_client: TestClient,
    upstream: MockUpstream,
    fetch_calls: list[tuple[str, int, int]],
    page: int | None,
) -> None:
    payload = request_body() if page is None else request_body(page=page)
    response = api_client.post(API_PATH, json=payload)
    expected_page = 1 if page is None else page
    assert response.status_code == 200
    assert response.json() == {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "page": expected_page,
        "per_page": 30,
        "files": [
            {
                "filename": "文档/新 文件.md",
                "status": "renamed",
                "additions": 4,
                "deletions": 2,
                "changes": 6,
                "previous_filename": "文档/旧 文件.md",
            }
        ],
    }
    assert "RAW_SECRET" not in response.text
    assert fetch_calls == [(REPOSITORY_URL, 42, expected_page)]
    assert len(upstream.requests) == 1
    request = upstream.requests[0]
    assert str(request.url.copy_with(query=None)) == (
        "https://api.github.com/repos/Sample-Owner/Sample-Repo/pulls/42/files"
    )
    assert sorted(request.url.params.multi_items()) == [
        ("page", str(expected_page)),
        ("per_page", "30"),
    ]
    assert request.method == "GET"
    assert request.headers["Accept-Encoding"] == "identity"
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("count", [0, 30])
def test_empty_and_full_pages_do_not_imply_another_request(
    api_client: TestClient, upstream: MockUpstream, count: int
) -> None:
    upstream.payload = [dict(file_payload(), filename=f"文件 {i}.png") for i in range(count)]
    upstream.headers = {"Link": '<https://evil.invalid/next>; rel="next"'}
    response = api_client.post(API_PATH, json=request_body(page=100))
    assert response.status_code == 200
    assert [item["filename"] for item in response.json()["files"]] == [
        f"文件 {i}.png" for i in range(count)
    ]
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=1)


def test_code_like_metadata_is_neither_reviewed_nor_executed(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    item = file_payload()
    item.update(filename="raise RuntimeError('metadata must not execute')", patch="print('data')")
    upstream.payload = [item]
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json()["files"][0]["filename"] == item["filename"]
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("explicit_null", [False, True])
def test_optional_previous_filename_is_returned_as_null(
    api_client: TestClient, upstream: MockUpstream, explicit_null: bool
) -> None:
    item = file_payload()
    if explicit_null:
        item["previous_filename"] = None
    else:
        del item["previous_filename"]
    upstream.payload = [item]
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json()["files"][0]["previous_filename"] is None
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize(
    "changes,location",
    [
        ({"repository_url": "http://github.com/a/b"}, "repository_url"),
        ({"repository_url": "https://github.com.evil.invalid/a/b"}, "repository_url"),
        ({"repository_url": "https://github.com/a/b?RAW_SECRET"}, "repository_url"),
        ({"repository_url": "\ud800RAW_SECRET"}, "repository_url"),
        ({"repository_url": 42}, "repository_url"),
        ({"pull_number": 0}, "pull_number"),
        ({"pull_number": 2_147_483_648}, "pull_number"),
        ({"pull_number": True}, "pull_number"),
        ({"pull_number": "42"}, "pull_number"),
        ({"pull_number": 42.0}, "pull_number"),
        ({"page": 0}, "page"),
        ({"page": 101}, "page"),
        ({"page": True}, "page"),
        ({"page": "1"}, "page"),
        ({"page": 1.0}, "page"),
        ({"page": None}, "page"),
        ({"per_page": 100}, "per_page"),
        ({"token": "RAW_SECRET"}, "token"),
    ],
)
def test_invalid_input_returns_safe_422_without_fetch(
    api_client: TestClient,
    upstream: MockUpstream,
    fetch_calls: list[tuple[str, int, int]],
    changes: dict[str, object],
    location: str,
) -> None:
    response = api_client.post(
        API_PATH,
        content=json.dumps(request_body(**changes), ensure_ascii=True),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert detail[0]["loc"] == ["body", location]
    assert set(detail[0]) == {"loc", "type", "msg"}
    assert "RAW_SECRET" not in response.text
    assert fetch_calls == []
    assert upstream.requests == []
    assert len(upstream.clients) in {0, 1}
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


@pytest.mark.parametrize("missing", ["repository_url", "pull_number"])
def test_identity_fields_are_required(
    api_client: TestClient,
    upstream: MockUpstream,
    fetch_calls: list[tuple[str, int, int]],
    missing: str,
) -> None:
    payload = request_body()
    del payload[missing]
    response = api_client.post(API_PATH, json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", missing]
    assert fetch_calls == []
    assert upstream.requests == []
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


def test_malformed_json_does_not_expose_raw_input(
    api_client: TestClient, upstream: MockUpstream, fetch_calls: list[tuple[str, int, int]]
) -> None:
    response = api_client.post(
        API_PATH, content=b'{"RAW_SECRET":', headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
    assert "RAW_SECRET" not in response.text
    assert fetch_calls == []
    assert upstream.requests == []
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


@pytest.mark.parametrize(
    "status,payload,headers,expected_status,detail",
    [
        (404, None, {}, 404, "仓库或 PR 不存在或不可公开访问。"),
        (429, None, {}, 503, "GitHub 请求受到限流，请稍后重试。"),
        (403, None, {"X-RateLimit-Remaining": "0"}, 503, "GitHub 请求受到限流，请稍后重试。"),
        (
            403,
            {"message": "secondary rate limit RAW_SECRET"},
            {},
            503,
            "GitHub 请求受到限流，请稍后重试。",
        ),
        (403, {"message": "RAW_SECRET forbidden"}, {}, 502, "GitHub 拒绝访问该仓库或 PR。"),
        (
            302,
            None,
            {"Location": "https://evil.invalid/RAW_SECRET"},
            502,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
        ),
        (500, None, {}, 502, UPSTREAM_MESSAGE),
        (200, None, {}, 502, UPSTREAM_MESSAGE),
        (200, [None], {}, 502, UPSTREAM_MESSAGE),
        (200, [dict(file_payload(), changes=True)], {}, 502, UPSTREAM_MESSAGE),
        (200, [dict(file_payload(), filename="RAW_SECRET\ud800")], {}, 502, UPSTREAM_MESSAGE),
        (200, [file_payload() for _ in range(31)], {}, 502, UPSTREAM_MESSAGE),
    ],
)
def test_real_client_errors_have_fixed_http_mapping(
    api_client: TestClient,
    upstream: MockUpstream,
    fetch_calls: list[tuple[str, int, int]],
    status: int,
    payload: object,
    headers: dict[str, str],
    expected_status: int,
    detail: str,
) -> None:
    upstream.status, upstream.payload, upstream.headers = status, payload, headers
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert fetch_calls == [(REPOSITORY_URL, 42, 1)]
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=1)


def test_oversized_body_stops_reading_and_returns_502(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    prefix = [b"x" * 1_048_576, b"x"]
    upstream.steps = [*prefix, httpx.ReadError("RAW_SECRET must not be consumed")]
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 502
    assert response.json() == {"detail": UPSTREAM_MESSAGE}
    assert upstream.streams[0].consumed == prefix
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("phase", ["connect", "read", "close"])
@pytest.mark.parametrize("timeout", [False, True])
def test_network_failures_release_created_resources(
    api_client: TestClient, upstream: MockUpstream, phase: str, timeout: bool
) -> None:
    error = httpx.ReadTimeout("RAW_SECRET") if timeout else httpx.ReadError("RAW_SECRET")
    if phase == "connect":
        upstream.connection_error = error
    elif phase == "read":
        upstream.steps = [b"[", error, b"must not be consumed"]
    else:
        upstream.close_error = error
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == (504 if timeout else 502)
    assert response.json() == {
        "detail": "GitHub 请求超时。" if timeout else "GitHub 网络请求失败。"
    }
    assert "RAW_SECRET" not in response.text
    assert_resources_closed(upstream, clients=1, responses=0 if phase == "connect" else 1)


@pytest.mark.parametrize("error_type,status,detail", ERROR_CASES)
def test_seven_exceptions_never_expose_original_text(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[GitHubClientError],
    status: int,
    detail: str,
) -> None:
    calls: list[tuple[str, int, int]] = []

    def fail_fetch(
        self: GitHubRepositoryClient, repository_url: str, pull_number: int, page: int
    ) -> NoReturn:
        calls.append((repository_url, pull_number, page))
        raise error_type("RAW_SECRET upstream details")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_files", fail_fetch)
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert calls == [(REPOSITORY_URL, 42, 1)]
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
def test_unexpected_errors_are_not_converted(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    original = error_type("unexpected programming error")

    def fail_fetch(*_args: object, **_kwargs: object) -> NoReturn:
        raise original

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_files", fail_fetch)
    with pytest.raises(error_type) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert_resources_closed(upstream, clients=1, responses=0)


def test_response_validation_is_not_wrapped_as_a_download_error(
    api_client: TestClient, upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid_result(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"files": []}

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_files", invalid_result)
    with pytest.raises(ResponseValidationError) as error:
        api_client.post(API_PATH, json=request_body())
    assert any(issue["loc"] == ("response", "repository_url") for issue in error.value.errors())
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize("scenario", ["success", "not-found", "invalid-response"])
def test_production_dependency_closes_distinct_clients_on_repeated_requests(
    api_client: TestClient, upstream: MockUpstream, scenario: str
) -> None:
    expected_status = 200
    if scenario == "not-found":
        upstream.status, expected_status = 404, 404
    elif scenario == "invalid-response":
        upstream.payload, expected_status = None, 502
    assert app.dependency_overrides == {}
    for count in (1, 2):
        response = api_client.post(API_PATH, json=request_body())
        assert response.status_code == expected_status
        assert_resources_closed(upstream, clients=count, responses=count)
    assert upstream.clients[0] is not upstream.clients[1]
    assert app.dependency_overrides == {}


def test_openapi_exposes_six_routes_and_strict_files_contract() -> None:
    with TestClient(app) as client:
        response = client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert {path: set(methods) for path, methods in document["paths"].items()} == {
        "/health": {"get"},
        "/api/v1/reviews": {"post"},
        "/api/v1/github/repository": {"post"},
        "/api/v1/github/file-review": {"post"},
        "/api/v1/github/pull-request": {"post"},
        API_PATH: {"post"},
    }
    operation = document["paths"][API_PATH]["post"]
    assert (
        operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/GitHubPullRequestFilesRequest"
    )
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/GitHubPullRequestFilesResponse"
    )
    schemas = document["components"]["schemas"]
    request = schemas["GitHubPullRequestFilesRequest"]
    assert set(request["properties"]) == {"repository_url", "pull_number", "page"}
    assert set(request["required"]) == {"repository_url", "pull_number"}
    assert request["additionalProperties"] is False
    assert request["properties"]["page"]["default"] == 1
    assert request["properties"]["page"]["minimum"] == 1
    assert request["properties"]["page"]["maximum"] == 100
    result = schemas["GitHubPullRequestFilesResponse"]
    assert (
        set(result["properties"])
        == set(result["required"])
        == {"repository_url", "pull_number", "page", "per_page", "files"}
    )
    assert result["additionalProperties"] is False
    assert (
        result["properties"]["per_page"]["minimum"]
        == result["properties"]["per_page"]["maximum"]
        == 30
    )
    assert result["properties"]["files"]["maxItems"] == 30
    assert (
        result["properties"]["files"]["items"]["$ref"]
        == "#/components/schemas/GitHubPullRequestFile"
    )
    item = schemas["GitHubPullRequestFile"]
    assert set(item["properties"]) == {
        "filename",
        "status",
        "additions",
        "deletions",
        "changes",
        "previous_filename",
    }
    assert set(item["required"]) == {"filename", "status", "additions", "deletions", "changes"}
    assert item["additionalProperties"] is False
    assert item["properties"]["previous_filename"]["anyOf"] == [
        {"type": "string", "minLength": 1},
        {"type": "null"},
    ]
    assert app.dependency_overrides == {}
