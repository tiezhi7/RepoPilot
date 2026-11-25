"""批量路由、真实客户端与审查器的离线集成；只模拟 GitHub 传输。"""

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
    GitHubPullRequestPythonFileContent,
    GitHubPullRequestPythonFiles,
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

API_PATH = "/api/v1/github/pull-request/batch-file-review"
REPOSITORY_URL = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
FILE_PATHS = ["src/z.py", "src/a.py", "Src/B.py"]
HEAD_SHA = "a1" * 20
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
    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("批量 API 测试禁止真实上游 HTTP 请求和 DNS 查询")

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
    sources: dict[str, str] = field(default_factory=lambda: {path: "pass\n" for path in FILE_PATHS})
    source_name: str = "contributor/sample-fork"
    payload_overrides: dict[str, object] = field(default_factory=dict)
    status_overrides: dict[str, int] = field(default_factory=dict)
    stream_steps: dict[str, list[bytes | Exception]] = field(default_factory=dict)
    close_errors: dict[str, httpx.RequestError] = field(default_factory=dict)
    fetch_error: Exception | None = None
    fetch_result: GitHubPullRequestPythonFiles | None = None
    fetch_calls: list[tuple[str, int, list[str]]] = field(default_factory=list)
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
                "title": "批量审查示例",
                "state": "open",
                "draft": False,
                "merged": False,
                "html_url": "https://github.com/Sample-Owner/Sample-Repo/pull/42",
                "base": {"ref": "main", "sha": "b2" * 20},
                "head": {
                    "ref": "feature/中文",
                    "sha": HEAD_SHA,
                    "repo": {"full_name": self.source_name, "private": False},
                },
                "changed_files": 0,
                "additions": 0,
                "deletions": 0,
                "diff_url": "http://127.0.0.1/RAW_SECRET",
            }
        source = self.sources[phase].encode("utf-8")
        return {
            "type": "file",
            "encoding": "base64",
            "size": len(source),
            "path": phase,
            "name": phase.rsplit("/", 1)[-1],
            "content": base64.b64encode(source).decode("ascii"),
            "download_url": "http://127.0.0.1/RAW_SECRET",
        }

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert all(response.is_closed for response in self.responses)
        if request.url.path == "/repos/Sample-Owner/Sample-Repo/pulls/42":
            phase = "pr"
            assert not request.url.params
        else:
            prefix = f"/repos/{self.source_name}/contents/"
            if not request.url.path.startswith(prefix):
                pytest.fail(f"非预期上游请求：{request.url}")
            phase = request.url.path.removeprefix(prefix)
            assert phase in self.sources
            assert dict(request.url.params) == {"ref": HEAD_SHA}
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
    assert all(client.is_closed for client in upstream.clients)
    assert all(transport.close_count == 1 for transport in upstream.transports)
    assert all(response.is_closed for response in upstream.responses)
    assert all(stream.close_count == 1 for stream in upstream.streams)


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> MockUpstream:
    value = MockUpstream()
    # 保留生产依赖的 with/yield，仅在构造客户端时注入离线传输。
    monkeypatch.setattr(main_module, "GitHubRepositoryClient", value.new_client)
    original_fetch = GitHubRepositoryClient.fetch_pull_request_python_files

    def fetch(
        client: GitHubRepositoryClient, repository_url: str, pull_number: int, file_paths: list[str]
    ) -> GitHubPullRequestPythonFiles:
        value.fetch_calls.append((repository_url, pull_number, file_paths.copy()))
        if value.fetch_error is not None:
            raise value.fetch_error
        if value.fetch_result is not None:
            return value.fetch_result
        return original_fetch(client, repository_url, pull_number, file_paths)

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("批量路由不得调用单文件方法或额外查询元数据、列表")

    monkeypatch.setattr(GitHubRepositoryClient, "fetch_pull_request_python_files", fetch)
    for name in (
        "fetch_repository",
        "fetch_python_file",
        "fetch_pull_request",
        "fetch_pull_request_files",
        "fetch_pull_request_python_file",
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
        assert client.is_closed


@pytest.fixture(autouse=True)
def review_inputs(monkeypatch: pytest.MonkeyPatch, upstream: MockUpstream) -> list[str]:
    inputs: list[str] = []

    def review(code: str) -> list[ReviewIssue]:
        # 即使只审查第一个文件，也必须已经关闭整批下载的所有响应。
        assert all(response.is_closed for response in upstream.responses)
        if upstream.fetch_calls:
            assert upstream.clients and not upstream.clients[-1].is_closed
        inputs.append(code)
        return review_python(code)

    monkeypatch.setattr(main_module, "review_python", review)
    return inputs


def request_body(**changes: object) -> dict[str, object]:
    return {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "file_paths": FILE_PATHS.copy(),
        **changes,
    }


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("same_repository", [False, True])
def test_exact_batch_response_pins_source_and_preserves_order(
    api_client: TestClient,
    upstream: MockUpstream,
    review_inputs: list[str],
    count: int,
    same_repository: bool,
) -> None:
    if same_repository:
        upstream.source_name = "Sample-Owner/Sample-Repo"
    paths = FILE_PATHS[:count]
    response = api_client.post(API_PATH, json=request_body(file_paths=paths))
    assert response.status_code == 200
    assert response.json() == {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "head_repository_url": f"https://github.com/{upstream.source_name}",
        "head_sha": HEAD_SHA,
        "files": [
            {"file_path": p, "review": {"language": "python", "issue_count": 0, "issues": []}}
            for p in paths
        ],
    }
    assert upstream.fetch_calls == [(REPOSITORY_URL, 42, paths)]
    assert review_inputs == [upstream.sources[path] for path in paths]
    assert len(upstream.requests) == count + 1
    assert [r.url.path for r in upstream.requests[1:]] == [
        f"/repos/{upstream.source_name}/contents/{p}" for p in paths
    ]
    for request in upstream.requests:
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        assert request.headers["Accept-Encoding"] == "identity"
    assert_resources_closed(upstream, clients=1, responses=count + 1)
    assert app.dependency_overrides == {}


def test_empty_unicode_and_raise_source_are_reviewed_once_without_execution(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    sources = ["", "# 中文\r\nvalue = '🙂'\r\n", "raise RuntimeError('RAW_SECRET')\n"]
    upstream.sources = dict(zip(FILE_PATHS, sources))
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    assert review_inputs == sources
    assert [item["review"] for item in response.json()["files"]] == [
        {"language": "python", "issue_count": 0, "issues": []}
    ] * 3
    assert "RAW_SECRET" not in response.text
    assert_resources_closed(upstream, clients=1, responses=4)


def test_rules_and_syntax_results_remain_per_file(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    code = "print('first')\neval('1')\ntry:\n    pass\nexcept:\n    print('last')\n"
    upstream.sources = dict(zip(FILE_PATHS, [code, "def broken(:\n", "pass\n"]))
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 200
    first, second, third = [item["review"] for item in response.json()["files"]]
    assert first["issue_count"] == len(first["issues"]) == 4
    assert [(x["rule_id"], x["severity"], x["line"]) for x in first["issues"]] == [
        ("PY_PRINT", "info", 1),
        ("PY_EVAL", "error", 2),
        ("PY_BARE_EXCEPT", "warning", 5),
        ("PY_PRINT", "info", 6),
    ]
    assert second["issue_count"] == len(second["issues"]) == 1
    assert second["issues"][0]["rule_id"] == "PY_SYNTAX"
    assert second["issues"][0]["severity"] == "error"
    assert second["issues"][0]["line"] == 1
    assert third == {"language": "python", "issue_count": 0, "issues": []}
    assert review_inputs == [code, "def broken(:\n", "pass\n"]
    assert_resources_closed(upstream, clients=1, responses=4)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", "https://github.com.evil.invalid/RAW_SECRET/repo"),
        ("repository_url", None),
        ("pull_number", True),
        ("pull_number", "42"),
        ("pull_number", 0),
        ("pull_number", 2_147_483_648),
        ("file_paths", None),
        ("file_paths", "a.py"),
        ("file_paths", []),
        ("file_paths", ["a.py", "b.py", "c.py", "d.py"]),
        ("file_paths", ["a.py", "a.py"]),
        ("file_paths", ["a.py", "../RAW_SECRET.py"]),
        ("file_paths", ["a.py", "src/示例.py"]),
        ("file_paths", ["a.py", "b.PY"]),
        ("file_paths", ["a.py", None]),
        ("file_paths", ["a.py", "\ud800.py"]),
        ("head_sha", "RAW_SECRET"),
        ("head_repository_url", "RAW_SECRET"),
        ("ref", "RAW_SECRET"),
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
    response = api_client.post(
        API_PATH,
        content=json.dumps(request_body(**{field: value}), ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    details = response.json()["detail"]
    assert isinstance(details, list) and details
    assert any(issue["loc"] == ["body", field] for issue in details)
    assert all(set(issue) == {"loc", "type", "msg"} for issue in details)
    assert "RAW_SECRET" not in response.text
    assert upstream.fetch_calls == upstream.requests == review_inputs == []
    assert_resources_closed(upstream, clients=len(upstream.clients), responses=0)


@pytest.mark.parametrize("field", ["repository_url", "pull_number", "file_paths"])
def test_missing_fields_never_fetch(
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
    assert upstream.fetch_calls == [(REPOSITORY_URL, 42, FILE_PATHS)]
    assert upstream.requests == review_inputs == []
    assert_resources_closed(upstream, clients=1, responses=0)


@pytest.mark.parametrize("phase", ["pr", *FILE_PATHS])
@pytest.mark.parametrize("kind", ["not-found", "read-timeout", "read-network", "close-timeout"])
def test_actual_failure_at_any_download_stage_prevents_all_review(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], phase: str, kind: str
) -> None:
    if kind == "not-found":
        upstream.status_overrides[phase] = 404
        status, detail = 404, "仓库、PR、提交或文件不存在或不可公开访问。"
    elif kind == "close-timeout":
        upstream.close_errors[phase] = httpx.ReadTimeout("RAW_SECRET")
        status, detail = 504, "GitHub 请求超时。"
    else:
        failure = httpx.ReadTimeout if kind == "read-timeout" else httpx.ReadError
        upstream.stream_steps[phase] = [b"{", failure("RAW_SECRET")]
        status, detail = (
            (504, "GitHub 请求超时。") if kind == "read-timeout" else (502, "GitHub 网络请求失败。")
        )
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "files" not in response.json() and "RAW_SECRET" not in response.text
    assert review_inputs == []
    assert len(upstream.fetch_calls) == 1
    count = 1 if phase == "pr" else FILE_PATHS.index(phase) + 2
    assert len(upstream.requests) == count
    assert_resources_closed(upstream, clients=1, responses=count)


@pytest.mark.parametrize("private", [False, True])
def test_unavailable_or_private_source_never_downloads_files(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], private: bool
) -> None:
    payload = upstream.payload("pr")
    if private:
        payload["head"]["repo"]["private"] = True
    else:
        payload["head"]["repo"] = None
    upstream.payload_overrides["pr"] = payload
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 422
    assert response.json() == {"detail": "PR 来源仓库不可用于公开文件审查。"}
    assert review_inputs == []
    assert len(upstream.requests) == 1
    assert_resources_closed(upstream, clients=1, responses=1)


@pytest.mark.parametrize("kind", ["too-large", "unsupported"])
def test_second_file_business_failure_returns_no_partial_results(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str], kind: str
) -> None:
    payload = upstream.payload(FILE_PATHS[1])
    if kind == "too-large":
        payload["size"] = 20_001
        expected = "Python 文件超过大小限制，最多允许 20000 字节。"
    else:
        payload["type"] = "symlink"
        expected = "GitHub 文件类型或编码不受支持。"
    upstream.payload_overrides[FILE_PATHS[1]] = payload
    response = api_client.post(API_PATH, json=request_body())
    assert response.status_code == 422
    assert response.json() == {"detail": expected}
    assert review_inputs == []
    assert len(upstream.requests) == 3
    assert_resources_closed(upstream, clients=1, responses=3)


@pytest.mark.parametrize("error_type", [GitHubClientError, ValueError, RuntimeError])
def test_unexpected_fetch_errors_are_not_disguised(
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
@pytest.mark.parametrize("failure_index", [0, 1])
def test_reviewer_errors_are_outside_download_mapping_even_after_prior_review(
    api_client: TestClient,
    upstream: MockUpstream,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    failure_index: int,
) -> None:
    original = error_type("reviewer failure")
    inputs: list[str] = []

    def fail_review(code: str) -> list[ReviewIssue]:
        inputs.append(code)
        if len(inputs) == failure_index + 1:
            raise original
        return []

    monkeypatch.setattr(main_module, "review_python", fail_review)
    with pytest.raises(error_type) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert inputs == [upstream.sources[p] for p in FILE_PATHS[: failure_index + 1]]
    assert len(upstream.fetch_calls) == 1 and len(upstream.requests) == 4
    assert_resources_closed(upstream, clients=1, responses=4)


def test_response_validation_error_is_not_a_download_failure(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    upstream.fetch_result = GitHubPullRequestPythonFiles(
        "https://github.com/contributor/sample-fork",
        "invalid SHA",
        tuple(GitHubPullRequestPythonFileContent(p, "pass\n") for p in FILE_PATHS),
    )
    with pytest.raises(ValidationError) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value.errors()[0]["loc"] == ("head_sha",)
    assert review_inputs == ["pass\n"] * 3
    assert_resources_closed(upstream, clients=1, responses=0)


def test_response_assembly_is_outside_download_mapping(
    api_client: TestClient, upstream: MockUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = GitHubUpstreamError("response assembly failure")

    def fail_assembly(*_args: object, **_kwargs: object) -> NoReturn:
        raise original

    monkeypatch.setattr(main_module, "GitHubPullRequestBatchFileReviewResponse", fail_assembly)
    with pytest.raises(GitHubUpstreamError) as error:
        api_client.post(API_PATH, json=request_body())
    assert error.value is original
    assert_resources_closed(upstream, clients=1, responses=4)


@pytest.mark.parametrize("status", [200, 404])
def test_production_dependency_closes_distinct_clients_without_overrides(
    api_client: TestClient, upstream: MockUpstream, status: int
) -> None:
    if status == 404:
        upstream.status_overrides[FILE_PATHS[1]] = 404
    assert app.dependency_overrides == {}
    for count in (1, 2):
        response = api_client.post(API_PATH, json=request_body())
        assert response.status_code == status
        assert_resources_closed(
            upstream, clients=count, responses=(4 if status == 200 else 3) * count
        )
    assert upstream.clients[0] is not upstream.clients[1]
    assert app.dependency_overrides == {}


def test_health_local_review_and_wrong_method_do_not_query_github(
    api_client: TestClient, upstream: MockUpstream
) -> None:
    assert api_client.get("/health").json() == {"status": "ok"}
    response = api_client.post("/api/v1/reviews", json={"code": "pass\n"})
    assert response.status_code == 200
    assert response.json() == {"language": "python", "issue_count": 0, "issues": []}
    assert api_client.get(API_PATH).status_code == 405
    assert upstream.fetch_calls == upstream.requests == []
    assert_resources_closed(upstream, clients=0, responses=0)


def test_openapi_exposes_eight_routes_and_exact_batch_contract(api_client: TestClient) -> None:
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
        "/api/v1/github/pull-request/file-review": {"post"},
        API_PATH: {"post"},
    }
    operation = document["paths"][API_PATH]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitHubPullRequestBatchFileReviewRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitHubPullRequestBatchFileReviewResponse"
    }
    schemas = document["components"]["schemas"]
    request = schemas["GitHubPullRequestBatchFileReviewRequest"]
    result = schemas["GitHubPullRequestBatchFileReviewResponse"]
    item = schemas["GitHubPullRequestBatchFileReviewItem"]
    for schema, fields in (
        (request, {"repository_url", "pull_number", "file_paths"}),
        (result, {"repository_url", "pull_number", "head_repository_url", "head_sha", "files"}),
        (item, {"file_path", "review"}),
    ):
        assert set(schema["properties"]) == set(schema["required"]) == fields
        assert schema["additionalProperties"] is False
    for schema, name in ((request, "file_paths"), (result, "files")):
        assert schema["properties"][name]["minItems"] == 1
        assert schema["properties"][name]["maxItems"] == 3
    assert item["properties"]["review"] == {"$ref": "#/components/schemas/ReviewResponse"}
    assert app.dependency_overrides == {}


def test_mutable_defaults_are_reviewed_per_file_after_all_downloads(
    api_client: TestClient, upstream: MockUpstream, review_inputs: list[str]
) -> None:
    sources = [
        "def f(\n    items=[],\n):\n    pass\n",
        "print('before')\ndef f(options={}):\n    return options\n",
        "def f(items=None):\n    return []\n",
    ]
    upstream.sources = dict(zip(FILE_PATHS, sources, strict=True))

    response = api_client.post(API_PATH, json=request_body())

    warning = {
        "rule_id": "PY_MUTABLE_DEFAULT",
        "severity": "warning",
        "line": 2,
        "message": "可变默认参数可能在多次调用间共享状态；建议使用 None，并在函数内创建新对象。",
    }
    reviews = [
        {"language": "python", "issue_count": 1, "issues": [warning]},
        {
            "language": "python",
            "issue_count": 2,
            "issues": [
                {
                    "rule_id": "PY_PRINT",
                    "severity": "info",
                    "line": 1,
                    "message": "发现 print() 调用；请确认是否需要保留调试输出。",
                },
                warning,
            ],
        },
        {"language": "python", "issue_count": 0, "issues": []},
    ]
    assert response.status_code == 200
    assert response.json() == {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "head_repository_url": f"https://github.com/{upstream.source_name}",
        "head_sha": HEAD_SHA,
        "files": [
            {"file_path": path, "review": review}
            for path, review in zip(FILE_PATHS, reviews, strict=True)
        ],
    }
    assert review_inputs == sources
    assert upstream.fetch_calls == [(REPOSITORY_URL, 42, FILE_PATHS)]
    assert len(upstream.requests) == 4
    assert [request.url.path for request in upstream.requests[1:]] == [
        f"/repos/{upstream.source_name}/contents/{path}" for path in FILE_PATHS
    ]
    assert all(dict(request.url.params) == {"ref": HEAD_SHA} for request in upstream.requests[1:])
    assert_resources_closed(upstream, clients=1, responses=4)
    assert app.dependency_overrides == {}
