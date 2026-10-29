"""使用真实客户端和审查器，离线验证 GitHub 文件审查路由及资源生命周期。"""

import base64
import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import NoReturn

import httpx
import pytest
from fastapi.testclient import TestClient

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
from app.reviewer import review_python
from app.schemas import ReviewIssue

API_PATH = "/api/v1/github/file-review"
REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
FILE_PATH = "src/api.py"


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """阻断真实传输和 DNS，保留 TestClient 事件循环所需的本地 socket 操作。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("文件 API 测试禁止真实 HTTP 请求和 DNS 查询")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_network)
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


class TrackingStream(httpx.SyncByteStream):
    """通过实际迭代和 close 记录，验证响应流关闭及中途读取失败。"""

    def __init__(self, steps: list[bytes | httpx.RequestError]) -> None:
        self.steps = steps
        self.consumed: list[bytes | httpx.RequestError] = []
        self.close_count = 0

    def __iter__(self) -> Iterator[bytes]:
        for step in self.steps:
            self.consumed.append(step)
            if isinstance(step, httpx.RequestError):
                raise step
            yield step

    def close(self) -> None:
        self.close_count += 1


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
    path: str = FILE_PATH
    handler: Callable[[httpx.Request], httpx.Response] | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)
    streams: list[TrackingStream] = field(default_factory=list)
    clients: list[GitHubRepositoryClient] = field(default_factory=list)
    transports: list[TrackingTransport] = field(default_factory=list)

    def file_payload(self) -> dict[str, object]:
        source = self.source.encode("utf-8")
        return {
            "type": "file",
            "encoding": "base64",
            "size": len(source),
            "path": self.path,
            "name": self.path.rsplit("/", 1)[-1],
            "content": base64.b64encode(source).decode("ascii"),
            "download_url": "http://127.0.0.1/RAW_SECRET",
            "url": "https://evil.invalid/RAW_SECRET",
            "_links": {"self": "https://evil.invalid/RAW_SECRET"},
        }

    def response(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        steps: list[bytes | httpx.RequestError] | None = None,
    ) -> httpx.Response:
        stream = TrackingStream([body] if steps is None else steps)
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    def json_response(self, payload: object) -> httpx.Response:
        return self.response(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = (
            self.handler(request)
            if self.handler is not None
            else self.json_response(self.file_payload())
        )
        self.responses.append(response)
        return response

    def new_client(self) -> GitHubRepositoryClient:
        transport = TrackingTransport(self.respond)
        client = GitHubRepositoryClient(transport=transport)
        self.transports.append(transport)
        self.clients.append(client)
        return client


def assert_resources_closed(upstream: MockUpstream) -> None:
    assert all(client.is_closed for client in upstream.clients)
    assert all(transport.close_count == 1 for transport in upstream.transports)
    assert all(response.is_closed for response in upstream.responses)
    assert all(stream.close_count == 1 for stream in upstream.streams)


@pytest.fixture
def upstream() -> MockUpstream:
    return MockUpstream()


@pytest.fixture
def api_client(upstream: MockUpstream) -> Iterator[TestClient]:
    def override_client() -> Iterator[GitHubRepositoryClient]:
        with upstream.new_client() as client:
            yield client

    app.dependency_overrides[get_github_repository_client] = override_client
    with TestClient(app) as client:
        yield client
    assert_resources_closed(upstream)


@dataclass
class ReviewSpy:
    """只记录调用，实际结果仍由原有 review_python 产生。"""

    inputs: list[str] = field(default_factory=list)
    results: list[list[ReviewIssue]] = field(default_factory=list)

    def review(self, code: str) -> list[ReviewIssue]:
        self.inputs.append(code)
        issues = review_python(code)
        self.results.append(issues)
        return issues


@pytest.fixture(autouse=True)
def review_spy(monkeypatch: pytest.MonkeyPatch) -> ReviewSpy:
    spy = ReviewSpy()
    monkeypatch.setattr("app.main.review_python", spy.review)
    return spy


@pytest.fixture
def forbid_file_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_fetch(self: GitHubRepositoryClient, repository_url: str, file_path: str) -> NoReturn:
        pytest.fail("无效请求不得调用文件读取方法")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_python_file", fail_fetch)


def test_normal_source_returns_the_exact_nested_response(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy
) -> None:
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == 200
    assert response.json() == {
        "repository_url": REPOSITORY_URL,
        "file_path": FILE_PATH,
        "review": {"language": "python", "issue_count": 0, "issues": []},
    }
    assert review_spy.inputs == [upstream.source]
    assert len(upstream.requests) == 1
    request = upstream.requests[0]
    assert request.method == "GET"
    assert (
        str(request.url)
        == "https://api.github.com/repos/sample-owner/sample-repo/contents/src/api.py"
    )
    assert request.url.query == b""
    assert "Authorization" not in request.headers
    assert "RAW_SECRET" not in response.text
    assert len(upstream.responses) == len(upstream.clients) == 1
    assert_resources_closed(upstream)


def test_rules_preserve_severity_line_order_and_original_issue_content(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy
) -> None:
    upstream.source = (
        "def nested():\n"
        "    print('inside')\n"
        "eval('1 + 1')\n"
        "try:\n"
        "    pass\n"
        "except:\n"
        "    pass\n"
        "print(eval('2'))\n"
    )
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == 200
    review = response.json()["review"]
    assert review["language"] == "python"
    assert review["issue_count"] == len(review["issues"]) == 5
    assert [(issue["rule_id"], issue["severity"], issue["line"]) for issue in review["issues"]] == [
        ("PY_PRINT", "info", 2),
        ("PY_EVAL", "error", 3),
        ("PY_BARE_EXCEPT", "warning", 6),
        ("PY_PRINT", "info", 8),
        ("PY_EVAL", "error", 8),
    ]
    assert review_spy.inputs == [upstream.source]
    assert review["issues"] == [issue.model_dump() for issue in review_spy.results[0]]


@pytest.mark.parametrize(
    "source",
    [
        "",
        "# 中文注释\nvalue = '你好'\n",
        "marker = 'SOURCE_MARKER_DO_NOT_RETURN'\n",
        "raise RuntimeError('SOURCE_MARKER_DO_NOT_RETURN')\n",
    ],
)
def test_empty_unicode_and_nonexecuted_source(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy, source: str
) -> None:
    upstream.source = source
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == 200
    assert response.json()["review"] == {"language": "python", "issue_count": 0, "issues": []}
    assert set(response.json()) == {"repository_url", "file_path", "review"}
    assert "SOURCE_MARKER_DO_NOT_RETURN" not in response.text
    assert review_spy.inputs == [source]


def test_syntax_error_is_a_successful_review_without_partial_rule_results(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy
) -> None:
    upstream.source = "print('before')\nif :\n    pass\n"
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == 200
    review = response.json()["review"]
    assert review["issue_count"] == len(review["issues"]) == 1
    assert review["issues"][0]["rule_id"] == "PY_SYNTAX"
    assert review["issues"][0]["severity"] == "error"
    assert review["issues"][0]["line"] == 2
    assert review["issues"] == [issue.model_dump() for issue in review_spy.results[0]]


def test_file_fetch_and_real_review_are_called_once_without_source_changes(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_url = "HTTPS://GitHub.Com/Example-Owner/Repo_Name/"
    upstream.path = "Src/My-File.py"
    upstream.source = "# 保留前后空白和换行\r\n\nprint('hello')\r\n"
    calls: list[tuple[str, str]] = []
    original_fetch = GitHubRepositoryClient.fetch_python_file

    def spy_fetch(self: GitHubRepositoryClient, url: str, path: str) -> str:
        calls.append((url, path))
        return original_fetch(self, url, path)

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_python_file", spy_fetch)
    response = api_client.post(
        API_PATH, json={"repository_url": repository_url, "file_path": upstream.path}
    )
    assert response.status_code == 200
    assert response.json()["repository_url"] == repository_url
    assert response.json()["file_path"] == upstream.path
    assert calls == [(repository_url, upstream.path)]
    assert review_spy.inputs == [upstream.source]
    assert response.json()["review"]["issues"][0]["line"] == 3


@pytest.mark.parametrize(
    ("repository_url", "path"),
    [
        (REPOSITORY_URL, ".py"),
        (REPOSITORY_URL, "a" * 509 + ".py"),
        ("https://github.com/a/" + "r" * (2048 - len("https://github.com/a/")), FILE_PATH),
    ],
)
def test_valid_url_and_path_length_boundaries(
    api_client: TestClient, upstream: MockUpstream, repository_url: str, path: str
) -> None:
    upstream.path = path
    response = api_client.post(API_PATH, json={"repository_url": repository_url, "file_path": path})
    assert response.status_code == 200
    assert response.json()["repository_url"] == repository_url
    assert response.json()["file_path"] == path


@pytest.mark.usefixtures("forbid_file_fetch")
@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("repository_url", 123, "string_type"),
        ("repository_url", True, "string_type"),
        ("repository_url", None, "string_type"),
        ("repository_url", [], "string_type"),
        ("repository_url", "", "string_too_short"),
        ("repository_url", "x" * 2049, "string_too_long"),
        ("file_path", 123, "string_type"),
        ("file_path", False, "string_type"),
        ("file_path", None, "string_type"),
        ("file_path", {}, "string_type"),
        ("file_path", "", "string_too_short"),
        ("file_path", "a" * 510 + ".py", "string_too_long"),
        ("token", "RAW_SECRET", "extra_forbidden"),
        ("ref", "RAW_SECRET", "extra_forbidden"),
    ],
)
def test_invalid_fields_keep_validation_format_and_skip_fetch_and_review(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    field_name: str,
    value: object,
    error_type: str,
) -> None:
    body: dict[str, object] = {"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    body[field_name] = value
    response = api_client.post(API_PATH, json=body)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert any(
        error["loc"] == ["body", field_name] and error["type"] == error_type for error in errors
    )
    assert "RAW_SECRET" not in response.text
    assert upstream.requests == []
    assert review_spy.inputs == []
    assert_resources_closed(upstream)


@pytest.mark.usefixtures("forbid_file_fetch")
@pytest.mark.parametrize("field_name", ["repository_url", "file_path"])
def test_missing_fields_are_rejected(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy, field_name: str
) -> None:
    body = {"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    del body[field_name]
    response = api_client.post(API_PATH, json=body)
    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"loc": ["body", field_name], "type": "missing", "msg": "Field required"}]
    }
    assert upstream.requests == []
    assert review_spy.inputs == []
    assert_resources_closed(upstream)


@pytest.mark.usefixtures("forbid_file_fetch")
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("repository_url", "http://github.com/a/b"),
        ("repository_url", "https://github.com.evil.invalid/a/b"),
        ("repository_url", "https://user:RAW_SECRET@github.com/a/b"),
        ("repository_url", "https://github.com:443/a/b"),
        ("repository_url", "https://github.com/a/b?token=RAW_SECRET"),
        ("file_path", "../RAW_SECRET.py"),
        ("file_path", "/RAW_SECRET.py"),
        ("file_path", "src//RAW_SECRET.py"),
        ("file_path", "src\\RAW_SECRET.py"),
        ("file_path", "%2e%2e/RAW_SECRET.py"),
        ("file_path", "api.py?RAW_SECRET"),
        ("file_path", "api.PY"),
        ("file_path", "api.txt"),
    ],
)
def test_dangerous_urls_and_paths_are_rejected_before_fetch(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    field_name: str,
    value: str,
) -> None:
    body = {"repository_url": REPOSITORY_URL, "file_path": FILE_PATH, field_name: value}
    response = api_client.post(API_PATH, json=body)
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert errors[0]["loc"] == ["body", field_name]
    assert "RAW_SECRET" not in response.text
    assert upstream.requests == []
    assert review_spy.inputs == []
    assert_resources_closed(upstream)


@pytest.mark.usefixtures("forbid_file_fetch")
@pytest.mark.parametrize(
    "body",
    [
        "",
        '{"repository_url":',
        r'{"repository_url":"\ud800","file_path":"api.py"}',
        r'{"repository_url":"https://github.com/a/b","file_path":"\ud800.py"}',
    ],
)
def test_missing_malformed_and_invalid_unicode_json(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy, body: str
) -> None:
    response = api_client.post(API_PATH, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert errors
    assert all(set(error) == {"loc", "type", "msg"} for error in errors)
    assert upstream.requests == []
    assert review_spy.inputs == []
    assert_resources_closed(upstream)


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected_status", "detail"),
    [
        (404, b"RAW_SECRET", {}, 404, "仓库或文件不存在或不可公开访问。"),
        (429, b"RAW_SECRET", {}, 503, "GitHub 请求受到限流，请稍后重试。"),
        (
            403,
            b"RAW_SECRET",
            {"X-RateLimit-Remaining": "0"},
            503,
            "GitHub 请求受到限流，请稍后重试。",
        ),
        (
            403,
            b'{"message":"API rate limit exceeded RAW_SECRET"}',
            {},
            503,
            "GitHub 请求受到限流，请稍后重试。",
        ),
        (
            403,
            b'{"message":"Forbidden RAW_SECRET"}',
            {},
            502,
            "GitHub 拒绝访问该仓库。",
        ),
        (
            302,
            b"RAW_SECRET",
            {"Location": "http://127.0.0.1/RAW_SECRET"},
            502,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
        ),
        (500, b"RAW_SECRET", {}, 502, "GitHub 返回了无效或非预期的文件响应。"),
        (200, b"RAW_SECRET", {}, 502, "GitHub 返回了无效或非预期的文件响应。"),
        # Contents API 的目录列表由客户端归为上游异常，路由不能重新分类。
        (200, b"[]", {}, 502, "GitHub 返回了无效或非预期的文件响应。"),
    ],
)
def test_real_client_maps_upstream_failures_without_running_reviewer(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    status: int,
    body: bytes,
    headers: dict[str, str],
    expected_status: int,
    detail: str,
) -> None:
    upstream.handler = lambda _request: upstream.response(body, status=status, headers=headers)
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert review_spy.inputs == []
    assert len(upstream.requests) == len(upstream.responses) == 1
    assert_resources_closed(upstream)


@pytest.mark.parametrize(
    ("case", "expected_status", "detail"),
    [
        ("large-file", 422, "Python 文件超过大小限制，最多允许 20000 字节。"),
        ("unsupported-encoding", 422, "GitHub 文件类型或编码不受支持。"),
        ("invalid-utf8", 422, "GitHub 文件类型或编码不受支持。"),
        ("large-response", 502, "GitHub 返回了无效或非预期的文件响应。"),
    ],
)
def test_file_business_errors_are_distinct_from_request_validation(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    case: str,
    expected_status: int,
    detail: str,
) -> None:
    payload = upstream.file_payload()
    if case == "large-file":
        payload["size"] = 20001
    elif case == "unsupported-encoding":
        payload["encoding"] = "none"
    elif case == "invalid-utf8":
        payload["size"] = 1
        payload["content"] = base64.b64encode(b"\xff").decode("ascii")
    else:
        upstream.handler = lambda _request: upstream.response(b"x" * 65537)
    if case != "large-response":
        upstream.handler = lambda _request: upstream.json_response(payload)

    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert review_spy.inputs == []
    assert len(upstream.requests) == len(upstream.responses) == 1
    assert_resources_closed(upstream)


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "detail"),
    [
        (httpx.ConnectTimeout, 504, "GitHub 请求超时。"),
        (httpx.ConnectError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_request_establishment_errors_release_the_client(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    exception_type: type[httpx.RequestError],
    expected_status: int,
    detail: str,
) -> None:
    def fail_request(request: httpx.Request) -> NoReturn:
        raise exception_type("RAW_SECRET", request=request)

    upstream.handler = fail_request
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert review_spy.inputs == []
    assert len(upstream.requests) == len(upstream.clients) == 1
    assert upstream.responses == []
    assert_resources_closed(upstream)


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "detail"),
    [
        (httpx.ReadTimeout, 504, "GitHub 请求超时。"),
        (httpx.ReadError, 502, "GitHub 网络请求失败。"),
    ],
)
def test_midstream_failures_close_the_response_and_skip_review(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    exception_type: type[httpx.RequestError],
    expected_status: int,
    detail: str,
) -> None:
    def interrupted_response(request: httpx.Request) -> httpx.Response:
        return upstream.response(
            b"",
            steps=[b'{"type":', exception_type("RAW_SECRET", request=request), b"unread"],
        )

    upstream.handler = interrupted_response
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert review_spy.inputs == []
    assert len(upstream.responses) == len(upstream.streams) == 1
    assert len(upstream.streams[0].consumed) == 2
    assert_resources_closed(upstream)


@pytest.mark.parametrize(
    ("exception_type", "expected_status", "detail"),
    [
        (GitHubNotFoundError, 404, "仓库或文件不存在或不可公开访问。"),
        (GitHubRateLimitError, 503, "GitHub 请求受到限流，请稍后重试。"),
        (GitHubForbiddenError, 502, "GitHub 拒绝访问该仓库。"),
        (
            GitHubRedirectError,
            502,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
        ),
        (GitHubUpstreamError, 502, "GitHub 返回了无效或非预期的文件响应。"),
        (GitHubTimeoutError, 504, "GitHub 请求超时。"),
        (GitHubNetworkError, 502, "GitHub 网络请求失败。"),
        (GitHubFileTooLargeError, 422, "Python 文件超过大小限制，最多允许 20000 字节。"),
        (GitHubUnsupportedFileError, 422, "GitHub 文件类型或编码不受支持。"),
    ],
)
def test_each_client_exception_has_fixed_safe_public_text(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[GitHubClientError],
    expected_status: int,
    detail: str,
) -> None:
    def fail_fetch(self: GitHubRepositoryClient, repository_url: str, file_path: str) -> NoReturn:
        raise exception_type("RAW_SECRET: headers, upstream body and internal details")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_python_file", fail_fetch)
    response = api_client.post(
        API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
    assert "RAW_SECRET" not in response.text
    assert REPOSITORY_URL not in response.text
    assert review_spy.inputs == []
    assert upstream.requests == []
    assert len(upstream.clients) == 1
    assert_resources_closed(upstream)


@pytest.mark.parametrize("exception_type", [GitHubClientError, ValueError, RuntimeError])
def test_unexpected_fetch_errors_are_not_caught_as_business_errors(
    api_client: TestClient,
    upstream: MockUpstream,
    review_spy: ReviewSpy,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
) -> None:
    def fail_fetch(self: GitHubRepositoryClient, repository_url: str, file_path: str) -> NoReturn:
        raise exception_type("test-only unexpected fetch failure")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_python_file", fail_fetch)
    with pytest.raises(exception_type, match="test-only unexpected fetch failure"):
        api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH})
    assert review_spy.inputs == []
    assert len(upstream.clients) == 1
    assert_resources_closed(upstream)


@pytest.mark.parametrize("exception_type", [RuntimeError, GitHubUpstreamError])
def test_reviewer_exceptions_are_outside_the_download_error_handler(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
) -> None:
    reviewed: list[str] = []

    def fail_review(code: str) -> NoReturn:
        reviewed.append(code)
        raise exception_type("test-only reviewer failure")

    monkeypatch.setattr("app.main.review_python", fail_review)
    with pytest.raises(exception_type, match="test-only reviewer failure"):
        api_client.post(API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH})
    assert reviewed == [upstream.source]
    assert len(upstream.responses) == len(upstream.clients) == 1
    assert_resources_closed(upstream)


@pytest.mark.parametrize("upstream_failure", [False, True])
def test_real_dependency_closes_distinct_clients_after_each_request(
    upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch, upstream_failure: bool
) -> None:
    # 不覆盖 Depends，保留生产环境的 with/yield，只把构造器中的传输换成 MockTransport。
    monkeypatch.setattr("app.main.GitHubRepositoryClient", upstream.new_client)
    assert get_github_repository_client not in app.dependency_overrides
    if upstream_failure:
        upstream.handler = lambda _request: upstream.response(b"RAW_SECRET", status=404)

    with TestClient(app) as client:
        for request_number in range(1, 3):
            response = client.post(
                API_PATH, json={"repository_url": REPOSITORY_URL, "file_path": FILE_PATH}
            )
            assert response.status_code == (404 if upstream_failure else 200)
            if upstream_failure:
                assert response.json() == {"detail": "仓库或文件不存在或不可公开访问。"}
            else:
                assert response.json()["review"]["issues"] == []
            assert len(upstream.clients) == len(upstream.responses) == request_number
            assert_resources_closed(upstream)
    assert upstream.clients[0] is not upstream.clients[1]
    assert get_github_repository_client not in app.dependency_overrides


def test_existing_health_review_and_repository_routes_remain_compatible(
    api_client: TestClient, upstream: MockUpstream, review_spy: ReviewSpy
) -> None:
    health = api_client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    local = api_client.post("/api/v1/reviews", json={"code": "total = 1 + 2"})
    assert local.status_code == 200
    assert local.json() == {"language": "python", "issue_count": 0, "issues": []}
    empty = api_client.post("/api/v1/reviews", json={"code": ""})
    assert empty.status_code == 422
    assert all(set(error) == {"loc", "type", "msg"} for error in empty.json()["detail"])
    assert upstream.clients == []

    raw_repository = {
        "full_name": "sample-owner/sample-repo",
        "description": None,
        "language": "Python",
        "stargazers_count": 42,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
        "extra": "RAW_SECRET",
    }
    upstream.handler = lambda _request: upstream.json_response(raw_repository)
    repository = api_client.post(
        "/api/v1/github/repository", json={"repository_url": REPOSITORY_URL}
    )
    assert repository.status_code == 200
    assert repository.json() == {
        "full_name": "sample-owner/sample-repo",
        "description": None,
        "language": "Python",
        "stars": 42,
        "default_branch": "main",
        "archived": False,
        "html_url": REPOSITORY_URL,
    }
    assert review_spy.inputs == ["total = 1 + 2"]
    assert len(upstream.requests) == 1
    assert str(upstream.requests[0].url) == "https://api.github.com/repos/sample-owner/sample-repo"
    assert_resources_closed(upstream)
