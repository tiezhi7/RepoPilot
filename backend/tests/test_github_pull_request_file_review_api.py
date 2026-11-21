"""真实路由、客户端与 AST 审查器配合 MockTransport 的离线集成测试。"""

import base64
import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import NoReturn

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main_module
from app.github_client import (
    GitHubClientError,
    GitHubFileTooLargeError,
    GitHubForbiddenError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubPullRequestPythonFile,
    GitHubPullRequestSourceUnavailableError,
    GitHubRateLimitError,
    GitHubRedirectError,
    GitHubRepositoryClient,
    GitHubTimeoutError,
    GitHubUnsupportedFileError,
    GitHubUpstreamError,
)
from app.main import app
from app.reviewer import review_python
from app.schemas import ReviewIssue

API_PATH = "/api/v1/github/pull-request/file-review"
REPOSITORY_URL = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
FILE_PATH = "src/example.py"
HEAD_SHA = "a1" * 20
SOURCE_URL = "https://github.com/contributor/sample-fork"
ERROR_CASES = [
    (GitHubNotFoundError, 404, "仓库、PR、提交或文件不存在或不可公开访问。"),
    (GitHubRateLimitError, 503, "GitHub 请求受到限流，请稍后重试。"),
    (GitHubForbiddenError, 502, "GitHub 拒绝访问该仓库、PR 或文件。"),
    (GitHubRedirectError, 502, "GitHub 返回了重定向，请使用仓库当前的主页地址。"),
    (GitHubUpstreamError, 502, "GitHub 返回了无效或非预期的 PR 文件读取响应。"),
    (GitHubTimeoutError, 504, "GitHub 请求超时。"),
    (GitHubNetworkError, 502, "GitHub 网络请求失败。"),
    (GitHubFileTooLargeError, 422, "Python 文件超过大小限制，最多允许 20000 字节。"),
    (GitHubUnsupportedFileError, 422, "GitHub 文件类型或编码不受支持。"),
    (GitHubPullRequestSourceUnavailableError, 422, "PR 来源仓库不可用于公开文件审查。"),
]


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断实际 HTTP 传输和 DNS，保留 TestClient 所需的本地事件循环。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 文件审查 API 测试禁止真实 HTTP 请求和 DNS 查询")

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


class TrackingStream(httpx.SyncByteStream):
    def __init__(
        self, steps: list[bytes | Exception], close_error: httpx.RequestError | None = None
    ) -> None:
        self.steps = steps
        self.close_error = close_error
        self.close_count = 0

    def __iter__(self) -> Iterator[bytes]:
        for step in self.steps:
            if isinstance(step, Exception):
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
    source: str = "total = 1 + 2\n"
    source_name: str = "contributor/sample-fork"
    payload_overrides: dict[str, object] = field(default_factory=dict)
    status_overrides: dict[str, int] = field(default_factory=dict)
    stream_steps: dict[str, list[bytes | Exception]] = field(default_factory=dict)
    connection_errors: dict[str, httpx.RequestError] = field(default_factory=dict)
    close_errors: dict[str, httpx.RequestError] = field(default_factory=dict)
    fetch_error: Exception | None = None
    fetch_result: GitHubPullRequestPythonFile | None = None
    fetch_calls: list[tuple[str, int, str]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)
    streams: list[TrackingStream] = field(default_factory=list)
    clients: list[GitHubRepositoryClient] = field(default_factory=list)
    transports: list[TrackingTransport] = field(default_factory=list)

    def payload(self, phase: str) -> object:
        if phase in self.payload_overrides:
            return self.payload_overrides[phase]
        if phase == "pr":
            return {
                "number": 42,
                "title": "修复示例",
                "state": "open",
                "draft": False,
                "merged": False,
                "html_url": "https://github.com/Sample-Owner/Sample-Repo/pull/42",
                "base": {"ref": "main", "sha": "b2" * 20},
                "head": {
                    "ref": "feature/中文分支",
                    "sha": HEAD_SHA,
                    "repo": {"full_name": self.source_name, "private": False},
                },
                "changed_files": 0,
                "additions": 0,
                "deletions": 0,
                "diff_url": "https://evil.invalid/RAW_SECRET",
            }
        source = self.source.encode("utf-8")
        return {
            "type": "file",
            "encoding": "base64",
            "size": len(source),
            "path": FILE_PATH,
            "name": "example.py",
            "content": base64.b64encode(source).decode("ascii"),
            "download_url": "http://127.0.0.1/RAW_SECRET",
        }

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/repos/Sample-Owner/Sample-Repo/pulls/42":
            phase = "pr"
        elif request.url.path == f"/repos/{self.source_name}/contents/{FILE_PATH}":
            phase = "file"
            assert self.responses[-1].is_closed
        else:
            pytest.fail(f"非预期上游请求：{request.url}")
        if phase in self.connection_errors:
            raise self.connection_errors[phase]
        body = json.dumps(self.payload(phase), ensure_ascii=True).encode("utf-8")
        stream = TrackingStream(self.stream_steps.get(phase, [body]), self.close_errors.get(phase))
        response = httpx.Response(self.status_overrides.get(phase, 200), stream=stream)
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
    # 仅替换构造器以注入传输，保留真实生产依赖中的 with/yield。
    monkeypatch.setattr(main_module, "GitHubRepositoryClient", value.new_client)
    original_fetch = GitHubRepositoryClient.fetch_pull_request_python_file

    def fetch(
        client: GitHubRepositoryClient, repository_url: str, pull_number: int, file_path: str
    ) -> GitHubPullRequestPythonFile:
        value.fetch_calls.append((repository_url, pull_number, file_path))
        if value.fetch_error is not None:
            raise value.fetch_error
        if value.fetch_result is not None:
            return value.fetch_result
        return original_fetch(client, repository_url, pull_number, file_path)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("新路由不得额外查询其他接口或退回默认分支")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_python_file", fetch)
    for name in (
        "fetch_repository",
        "fetch_python_file",
        "fetch_pull_request",
        "fetch_pull_request_files",
    ):
        monkeypatch.setattr(GitHubRepositoryClient, name, forbidden)
    return value


@pytest.fixture
def api_client(upstream: MockUpstream) -> Iterator[TestClient]:
    client = TestClient(app)
    try:
        with client:
            yield client
    finally:
        client.close()


@pytest.fixture(autouse=True)
def review_inputs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    inputs: list[str] = []

    def review(code: str) -> list[ReviewIssue]:
        inputs.append(code)
        return review_python(code)

    monkeypatch.setattr(main_module, "review_python", review)
    return inputs


def request_body(**changes: object) -> dict[str, object]:
    return {"repository_url": REPOSITORY_URL, "pull_number": 42, "file_path": FILE_PATH, **changes}


@pytest.mark.parametrize("same_repository", [True, False])
def test_exact_response_uses_resolved_source_and_sha(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], same_repository: bool
) -> None:
    if same_repository:
        upstream.source_name = "Sample-Owner/Sample-Repo"
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json() == {
        **request_body(),
        "head_repository_url": f"https://github.com/{upstream.source_name}",
        "head_sha": HEAD_SHA,
        "review": {"language": "python", "issue_count": 0, "issues": []},
    }
    assert upstream.fetch_calls == [(REPOSITORY_URL, 42, FILE_PATH)]
    assert review_inputs == [upstream.source]
    assert len(upstream.requests) == 2
    assert upstream.requests[0].url.params == httpx.QueryParams()
    assert dict(upstream.requests[1].url.params) == {"ref": HEAD_SHA}
    for request in upstream.requests:
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.url.scheme == "https"
        assert request.headers["Accept-Encoding"] == "identity"
    assert "code" not in response.json()
    assert app.dependency_overrides == {}
    assert_resources_closed(upstream, clients=1, responses=2)


@pytest.mark.parametrize(
    "source", ["", "# 中文\r\nvalue = '🙂'\r\n", "raise RuntimeError('RAW_SECRET')\n"]
)
def test_complete_source_is_reviewed_once_without_execution(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], source: str
) -> None:
    upstream.source = source
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert response.json()["review"] == {"language": "python", "issue_count": 0, "issues": []}
    assert review_inputs == [source]
    assert len(upstream.fetch_calls) == 1
    assert_resources_closed(upstream, clients=1, responses=2)


def test_rule_severity_line_order_and_count(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    upstream.source = "print('first')\neval('1')\ntry:\n    pass\nexcept:\n    print('last')\n"
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    review = response.json()["review"]
    assert review["language"] == "python"
    assert review["issue_count"] == len(review["issues"]) == 4
    assert [(x["rule_id"], x["severity"], x["line"]) for x in review["issues"]] == [
        ("PY_PRINT", "info", 1),
        ("PY_EVAL", "error", 2),
        ("PY_BARE_EXCEPT", "warning", 5),
        ("PY_PRINT", "info", 6),
    ]
    assert all(isinstance(x["message"], str) and x["message"] for x in review["issues"])
    assert review_inputs == [upstream.source]
    assert_resources_closed(upstream, clients=1, responses=2)


def test_syntax_error_remains_a_successful_review(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    upstream.source = "def ("
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    review = response.json()["review"]
    assert review["issue_count"] == len(review["issues"]) == 1
    assert review["issues"][0]["rule_id"] == "PY_SYNTAX"
    assert review["issues"][0]["severity"] == "error"
    assert review["issues"][0]["line"] == 1
    assert review_inputs == [upstream.source]
    assert_resources_closed(upstream, clients=1, responses=2)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", "https://github.com.evil.invalid/owner/repo"),
        ("repository_url", None),
        ("pull_number", 0),
        ("pull_number", True),
        ("pull_number", "42"),
        ("pull_number", 42.0),
        ("file_path", "../example.py"),
        ("file_path", "src/示例.py"),
        ("file_path", "src/example.PY"),
        ("file_path", None),
        ("head_sha", "a1" * 20),
        ("head_repository_url", SOURCE_URL),
        ("token", "RAW_SECRET"),
    ],
)
def test_invalid_input_never_fetches_or_reviews(
    api_client: TestClient,
    upstream: MockUpstream,
    review_inputs: list[str],
    field: str,
    value: object,
) -> None:
    response = api_client.post(API_PATH, json=request_body(**{field: value}))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any(issue["loc"] == ["body", field] for issue in detail)
    assert all(set(issue) == {"loc", "type", "msg"} for issue in detail)
    assert "RAW_SECRET" not in response.text
    assert upstream.fetch_calls == upstream.requests == review_inputs == []
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


@pytest.mark.parametrize("field", ["repository_url", "pull_number", "file_path"])
def test_missing_fields_are_rejected_before_fetch(
    api_client: TestClient, upstream: MockUpstream, field: str
) -> None:
    body = request_body()
    del body[field]
    response = api_client.post(API_PATH, json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert upstream.fetch_calls == upstream.requests == []
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


@pytest.mark.parametrize(("error_type", "status", "detail"), ERROR_CASES)
def test_ten_download_errors_have_fixed_sanitized_responses(
    api_client: TestClient,
    upstream: MockUpstream,
    review_inputs: list[str],
    error_type: type[Exception],
    status: int,
    detail: str,
) -> None:
    upstream.fetch_error = error_type("RAW_SECRET")
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert upstream.fetch_calls == [(REPOSITORY_URL, 42, FILE_PATH)]
    assert upstream.requests == review_inputs == []
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize("phase", ["pr", "file"])
@pytest.mark.parametrize("kind", ["not-found", "read-timeout", "read-network", "close-timeout"])
def test_actual_download_errors_release_resources_without_review(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], phase: str, kind: str
) -> None:
    if kind == "not-found":
        upstream.status_overrides[phase] = 404
        expected_status = 404
    elif kind == "close-timeout":
        upstream.close_errors[phase] = httpx.ReadTimeout("RAW_SECRET")
        expected_status = 504
    else:
        error_type = httpx.ReadTimeout if kind == "read-timeout" else httpx.ReadError
        upstream.stream_steps[phase] = [b"{", error_type("RAW_SECRET")]
        expected_status = 504 if kind == "read-timeout" else 502
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == expected_status
    assert isinstance(response.json()["detail"], str)
    assert "RAW_SECRET" not in response.text
    assert review_inputs == []
    assert len(upstream.fetch_calls) == 1
    responses = 1 if phase == "pr" else 2
    assert len(upstream.requests) == responses
    assert_resources_closed(upstream, clients=1, responses=responses)


def test_deleted_source_has_business_422_without_file_request(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    payload = upstream.payload("pr")
    payload["head"]["repo"] = None
    upstream.payload_overrides["pr"] = payload
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 422
    assert response.json() == {"detail": "PR 来源仓库不可用于公开文件审查。"}
    assert review_inputs == []
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("kind", ["too-large", "unsupported"])
def test_file_business_422_is_a_string_detail(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], kind: str
) -> None:
    payload = upstream.payload("file")
    if kind == "too-large":
        payload["size"] = 20_001
        expected = "Python 文件超过大小限制，最多允许 20000 字节。"
    else:
        payload["type"] = "symlink"
        expected = "GitHub 文件类型或编码不受支持。"
    upstream.payload_overrides["file"] = payload
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 422
    assert response.json() == {"detail": expected}
    assert review_inputs == []
    assert_resources_closed(upstream, clients=1, responses=2)


@pytest.mark.parametrize("error_type", [GitHubClientError, ValueError, RuntimeError])
def test_unexpected_fetch_errors_are_not_wrapped(
    api_client: TestClient, upstream: MockUpstream, error_type: type[Exception]
) -> None:
    original = error_type("unexpected internal failure")
    upstream.fetch_error = original
    with pytest.raises(error_type) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert len(upstream.fetch_calls) == 1
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize("error_type", [case[0] for case in ERROR_CASES])
def test_reviewer_errors_are_outside_download_error_mapping(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    original = error_type("reviewer failure")
    inputs: list[str] = []

    def fail_review(code: str) -> NoReturn:
        inputs.append(code)
        raise original

    monkeypatch.setattr(main_module, "review_python", fail_review)
    with pytest.raises(error_type) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert inputs == [upstream.source]
    assert len(upstream.fetch_calls) == 1
    assert_resources_closed(upstream, clients=1, responses=2)


def test_response_validation_is_not_reported_as_download_failure(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    upstream.fetch_result = GitHubPullRequestPythonFile(SOURCE_URL, "invalid SHA", "pass\n")
    with pytest.raises(ValidationError) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value.errors()[0]["loc"] == ("head_sha",)
    assert review_inputs == ["pass\n"]
    assert_resources_closed(upstream, clients=1, responses=0)


def test_response_assembly_errors_are_outside_download_mapping(
    api_client: TestClient, upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = GitHubUpstreamError("response assembly failure")

    def fail_assembly(*_args: object, **_kwargs: object) -> NoReturn:
        raise original

    monkeypatch.setattr(main_module, "GitHubPullRequestFileReviewResponse", fail_assembly)
    with pytest.raises(GitHubUpstreamError) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert_resources_closed(upstream, clients=1, responses=2)


@pytest.mark.parametrize("status", [200, 404])
def test_production_dependency_uses_and_closes_distinct_clients(
    api_client: TestClient, upstream: MockUpstream, status: int
) -> None:
    if status == 404:
        upstream.status_overrides["file"] = 404
    assert app.dependency_overrides == {}
    for count in (1, 2):
        response = api_client.post(API_PATH, json=request_body())
        assert response.status_code == status
        assert_resources_closed(upstream, clients=count, responses=2 * count)
    assert upstream.clients[0] is not upstream.clients[1]
    assert app.dependency_overrides == {}


def test_old_local_routes_and_wrong_method_remain_compatible(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    assert api_client.get("/health").json() == {"status": "ok"}
    local = api_client.post("/api/v1/reviews", json={"code": "pass\n"})
    assert local.status_code == 200
    assert local.json() == {"language": "python", "issue_count": 0, "issues": []}
    assert api_client.get(API_PATH).status_code == 405
    assert upstream.fetch_calls == upstream.requests == []
    assert_resources_closed(upstream, clients=0, responses=0)


def test_openapi_exposes_seven_routes_and_exact_review_contract(api_client: TestClient) -> None:
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert {path: set(methods) for path, methods in document["paths"].items()} == {
        "/health": {"get"},
        "/api/v1/reviews": {"post"},
        "/api/v1/github/repository": {"post"},
        "/api/v1/github/file-review": {"post"},
        "/api/v1/github/pull-request": {"post"},
        "/api/v1/github/pull-request/files": {"post"},
        API_PATH: {"post"},
    }
    operation = document["paths"][API_PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitHubPullRequestFileReviewRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitHubPullRequestFileReviewResponse"
    }
    schemas = document["components"]["schemas"]
    request = schemas["GitHubPullRequestFileReviewRequest"]
    assert (
        set(request["properties"])
        == set(request["required"])
        == {"repository_url", "pull_number", "file_path"}
    )
    assert request["additionalProperties"] is False
    assert request["properties"]["pull_number"]["minimum"] == 1
    assert request["properties"]["pull_number"]["maximum"] == 2_147_483_647
    assert request["properties"]["file_path"]["maxLength"] == 512
    result = schemas["GitHubPullRequestFileReviewResponse"]
    assert (
        set(result["properties"])
        == set(result["required"])
        == {
            "repository_url",
            "pull_number",
            "file_path",
            "head_repository_url",
            "head_sha",
            "review",
        }
    )
    assert result["additionalProperties"] is False
    assert result["properties"]["head_sha"]["minLength"] == 40
    assert result["properties"]["head_sha"]["maxLength"] == 40
    assert result["properties"]["review"] == {"$ref": "#/components/schemas/ReviewResponse"}
    assert app.dependency_overrides == {}
