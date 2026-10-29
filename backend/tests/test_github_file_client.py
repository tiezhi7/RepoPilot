"""使用模拟传输和可追踪字节流验证文件读取；不访问 GitHub，不执行源码。"""

import base64
import json
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import NoReturn

import httpx
import pytest

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

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
FILE_PATH = "src/api.py"
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的文件响应。"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件独立阻断 DNS 和真实 socket 连接，不借用其他测试的 fixture。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("文件客户端测试禁止真实 DNS 和网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


def file_payload(source: bytes = b"print('hello')\n", path: str = FILE_PATH) -> dict[str, object]:
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(source),
        "path": path,
        "name": path.rsplit("/", 1)[-1],
        "content": base64.b64encode(source).decode("ascii"),
    }


def json_body(payload: object) -> bytes:
    # 显式序列化，确保 None 表示 JSON null，而不是空响应体。
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TrackingStream(httpx.SyncByteStream):
    """记录被消费的每一步和关闭次数，也能在读取或关闭时模拟错误。"""

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
    stream: TrackingStream
    status: int
    headers: dict[str, str]
    requests: list[httpx.Request] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = httpx.Response(self.status, headers=self.headers, stream=self.stream)
        self.responses.append(response)
        return response


@contextmanager
def mock_file_response(
    body: bytes = b"",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    stream: TrackingStream | None = None,
) -> Iterator[tuple[GitHubRepositoryClient, MockUpstream]]:
    upstream = MockUpstream(stream or TrackingStream([body]), status, headers or {})
    with GitHubRepositoryClient(transport=TrackingTransport(upstream.respond)) as client:
        yield client, upstream


def assert_response_closed(client: GitHubRepositoryClient, upstream: MockUpstream) -> None:
    assert len(upstream.responses) == 1
    assert upstream.responses[0].is_closed
    assert upstream.stream.close_count == 1
    assert not client.is_closed


@pytest.mark.parametrize(
    "source",
    ["print('hello')\n", "# 中文注释\nprint('你好')\r\n", "", "def broken(:\n", "\ufeff# BOM\n"],
)
def test_source_is_returned_unchanged(source: str) -> None:
    with mock_file_response(json_body(file_payload(source.encode("utf-8")))) as (client, upstream):
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == source
        assert len(upstream.requests) == 1
        assert_response_closed(client, upstream)


def test_base64_allows_only_cr_and_lf_cleanup() -> None:
    payload = file_payload()
    content = str(payload["content"])
    payload["content"] = "\r\n" + content[:4] + "\r" + content[4:8] + "\n" + content[8:] + "\n"
    with mock_file_response(json_body(payload)) as (client, upstream):
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("repository_url", "path", "expected_path"),
    [
        (REPOSITORY_URL, FILE_PATH, "/repos/sample-owner/sample-repo/contents/src/api.py"),
        (
            "HTTPS://GitHub.Com/PsF/ReQuEsTs/",
            ".hidden.py",
            "/repos/PsF/ReQuEsTs/contents/.hidden.py",
        ),
    ],
)
def test_single_request_target_headers_and_timeouts(
    repository_url: str, path: str, expected_path: str
) -> None:
    payload = file_payload(path=path)
    payload.update(
        download_url="http://127.0.0.1/private",
        url="https://evil.invalid/file",
        git_url="https://evil.invalid/git",
        html_url="https://evil.invalid/html",
        _links={"self": "https://evil.invalid/link"},
        submodule_git_url=None,
    )
    with mock_file_response(json_body(payload)) as (client, upstream):
        assert client.fetch_python_file(repository_url, path) == "print('hello')\n"
        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request.method == "GET"
        assert str(request.url) == "https://api.github.com" + expected_path
        assert request.url.path == expected_path
        assert request.url.query == b""
        assert request.headers["Host"] == "api.github.com"
        assert request.headers["User-Agent"] == "RepoPilot"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert request.headers["Accept-Encoding"] == "identity"
        assert "Authorization" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 3.0,
            "read": 5.0,
            "write": 5.0,
            "pool": 5.0,
        }
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("repository_url", "path"),
    [
        (None, FILE_PATH),
        (123, FILE_PATH),
        (True, FILE_PATH),
        (b"https://github.com/a/b", FILE_PATH),
        (REPOSITORY_URL, None),
        (REPOSITORY_URL, False),
        (REPOSITORY_URL, 123),
        (REPOSITORY_URL, b"api.py"),
        ("http://github.com/a/b", FILE_PATH),
        ("https://github.com.evil.invalid/a/b", FILE_PATH),
        ("https://user@github.com/a/b", FILE_PATH),
        ("https://github.com:443/a/b", FILE_PATH),
        ("https://github.com/a/b?ref=main", FILE_PATH),
        (REPOSITORY_URL, "../secret.py"),
        (REPOSITORY_URL, "src/./api.py"),
        (REPOSITORY_URL, "/api.py"),
        (REPOSITORY_URL, "src//api.py"),
        (REPOSITORY_URL, "src\\api.py"),
        (REPOSITORY_URL, "src%2fapi.py"),
        (REPOSITORY_URL, "api.py?ref=main"),
        (REPOSITORY_URL, "api.PY"),
    ],
)
def test_invalid_input_never_reaches_transport(repository_url: str, path: str) -> None:
    with mock_file_response() as (client, upstream):
        with pytest.raises(ValueError) as error:
            client.fetch_python_file(repository_url, path)
        assert str(error.value) == "仓库地址或 Python 文件路径无效。"
        assert error.value.__suppress_context__
        assert upstream.requests == []
        assert upstream.stream.consumed == []


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (404, GitHubNotFoundError, "仓库或文件不存在或不可公开访问。"),
        (429, GitHubRateLimitError, "GitHub 请求受到限流，请稍后重试。"),
        (201, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (204, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (400, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (401, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (500, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (503, GitHubUpstreamError, UPSTREAM_MESSAGE),
    ],
)
def test_status_errors_do_not_read_body(
    status: int, error_type: type[GitHubClientError], message: str
) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET body must not be read")])
    with mock_file_response(status=status, stream=stream, headers={"Content-Encoding": "gzip"}) as (
        client,
        upstream,
    ):
        with pytest.raises(error_type) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == message
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 305, 307, 308, 399])
@pytest.mark.parametrize("location", ["https://evil.invalid/file", "http://127.0.0.1/private"])
def test_redirects_never_read_or_follow_the_location(status: int, location: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET unexpected read")])
    with mock_file_response(status=status, headers={"Location": location}, stream=stream) as (
        client,
        upstream,
    ):
        with pytest.raises(GitHubRedirectError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "GitHub 返回了重定向，请使用仓库当前的主页地址。"
        assert len(upstream.requests) == 1
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("remaining", ["0", " 0 "])
def test_403_zero_remaining_does_not_read_body(remaining: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET unexpected read")])
    with mock_file_response(
        status=403, headers={"X-RateLimit-Remaining": remaining}, stream=stream
    ) as (client, upstream):
        with pytest.raises(GitHubRateLimitError):
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "message", ["API rate limit exceeded", "You exceeded a SECONDARY RATE LIMIT."]
)
def test_403_explicit_message_is_rate_limited(message: str) -> None:
    with mock_file_response(json_body({"message": message}), status=403) as (client, upstream):
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "GitHub 请求受到限流，请稍后重试。"
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "body",
    [
        b"null",
        b"[]",
        b"{invalid RAW_SECRET",
        b"\xff",
        b"{}",
        json_body({"message": None}),
        json_body({"message": 123}),
        json_body({"message": "Forbidden RAW_SECRET"}),
    ],
)
def test_ordinary_403_remains_forbidden(body: bytes) -> None:
    with mock_file_response(
        body, status=403, headers={"X-RateLimit-Remaining": "8", "Retry-After": "60"}
    ) as (client, upstream):
        with pytest.raises(GitHubForbiddenError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "GitHub 拒绝访问该仓库。"
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{invalid RAW_SECRET",
        b"\xff",
        b"null",
        b"[]",
        b'"RAW_SECRET"',
        b"123",
        b"true",
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_invalid_or_non_object_json_is_rejected(body: bytes) -> None:
    with mock_file_response(body) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("field_name", ["type", "encoding", "size", "path", "name", "content"])
def test_every_file_field_is_required(field_name: str) -> None:
    payload = file_payload()
    del payload[field_name]
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("type", None),
        ("type", 123),
        ("encoding", None),
        ("encoding", False),
        ("size", True),
        ("size", False),
        ("size", -1),
        ("size", 1.0),
        ("size", "1"),
        ("size", None),
        ("path", None),
        ("path", 123),
        ("name", None),
        ("name", []),
        ("content", None),
        ("content", 123),
        ("path", "other/api.py"),
        ("name", "other.py"),
    ],
)
def test_invalid_fields_and_identity_are_rejected(field_name: str, value: object) -> None:
    payload = file_payload()
    payload[field_name] = value
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "dir"},
        {"type": "symlink"},
        {"type": "submodule"},
        {"encoding": "none"},
        {"target": "somewhere.py"},
        {"target": None},
        {"submodule_git_url": "https://evil.invalid/module"},
        {"submodule_git_url": False},
    ],
)
def test_unsupported_types_encodings_and_markers(changes: dict[str, object]) -> None:
    payload = file_payload()
    payload.update(changes)
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubUnsupportedFileError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "GitHub 文件类型或编码不受支持。"
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("content", ["!!!!", "中文", "YQ", "Y===", "Y Q==", "YQ==\t"])
def test_invalid_base64_is_rejected_without_repairs(content: str) -> None:
    payload = file_payload(b"a")
    payload["content"] = content
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert_response_closed(client, upstream)


def test_non_utf8_source_is_unsupported() -> None:
    with mock_file_response(json_body(file_payload(b"\xff\xfe"))) as (client, upstream):
        with pytest.raises(GitHubUnsupportedFileError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "GitHub 文件类型或编码不受支持。"
        assert error.value.__suppress_context__
        assert_response_closed(client, upstream)


def test_exact_source_and_base64_limits_are_accepted() -> None:
    source = b"#" + b"a" * 19_999
    payload = file_payload(source)
    assert payload["size"] == 20_000
    assert len(str(payload["content"])) == 26_668
    with mock_file_response(json_body(payload)) as (client, upstream):
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == source.decode("utf-8")
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("declared_size", [20_001, 20_000, 0])
def test_oversized_source_cannot_hide_behind_a_smaller_size(declared_size: int) -> None:
    payload = file_payload(b"a" * 20_001)
    # 20001 字节仍只有 26668 个 Base64 字符，必须另外检查真实解码字节数。
    assert len(str(payload["content"])) == 26_668
    payload["size"] = declared_size
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubFileTooLargeError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == "Python 文件超过大小限制。"
        assert_response_closed(client, upstream)


def test_declared_oversize_is_rejected_before_base64_decoding() -> None:
    payload = file_payload()
    payload.update(size=20_001, content="INVALID RAW_SECRET")
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubFileTooLargeError):
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("line_breaks", [False, True])
def test_base64_character_limit_is_checked_after_cr_lf_cleanup(line_breaks: bool) -> None:
    content = "A" * 26_669
    if line_breaks:
        content = "\r\n" + content[:100] + "\n" + content[100:]
    payload = file_payload(b"")
    payload["content"] = content
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubFileTooLargeError):
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(("source", "declared_size"), [(b"a", 0), (b"a", 2), (b"", 1)])
def test_declared_size_must_match_actual_bytes(source: bytes, declared_size: int) -> None:
    payload = file_payload(source)
    payload["size"] = declared_size
    with mock_file_response(json_body(payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_exact_raw_response_limit_ignores_content_length(headers: dict[str, str]) -> None:
    body = json_body(file_payload())
    body += b" " * (65_536 - len(body))
    stream = TrackingStream([body[:100], body[100:]])
    with mock_file_response(headers=headers, stream=stream) as (client, upstream):
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert stream.consumed == stream.steps
        assert_response_closed(client, upstream)
        if not headers:
            assert "Content-Length" not in upstream.responses[0].headers


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
def test_raw_overflow_stops_before_consuming_later_chunks(
    status: int, headers: dict[str, str]
) -> None:
    stream = TrackingStream(
        [b" " * 32_768, b" " * 32_768, b"x", httpx.ReadError("must not consume this marker")]
    )
    with mock_file_response(status=status, headers=headers, stream=stream) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert stream.consumed == stream.steps[:3]
        assert_response_closed(client, upstream)


def test_a_single_oversized_chunk_is_not_followed_by_more_reads() -> None:
    stream = TrackingStream([b"x" * 65_537, b"must not be consumed"])
    with mock_file_response(stream=stream) as (client, upstream):
        with pytest.raises(GitHubUpstreamError):
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert stream.consumed == stream.steps[:1]
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", ""])
def test_http_encoding_is_rejected_before_body_read(status: int, encoding: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not read encoded body")])
    with mock_file_response(
        status=status, headers={"Content-Encoding": encoding}, stream=stream
    ) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == UPSTREAM_MESSAGE
        assert stream.consumed == []
        assert_response_closed(client, upstream)


def test_explicit_identity_response_encoding_is_accepted() -> None:
    with mock_file_response(
        json_body(file_payload()), headers={"Content-Encoding": "identity"}
    ) as (client, upstream):
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("httpx_error", "client_error", "message"),
    [
        (httpx.ConnectTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.WriteTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.PoolTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ConnectError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RemoteProtocolError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_request_establishment_errors_are_safe(
    httpx_error: type[httpx.RequestError], client_error: type[GitHubClientError], message: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx_error("RAW_SECRET request details", request=request)

    transport = TrackingTransport(handler)
    with GitHubRepositoryClient(transport=transport) as client:
        with pytest.raises(client_error) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == message
        assert error.value.__suppress_context__
        assert len(requests) == 1
        assert not client.is_closed
    assert client.is_closed
    assert transport.close_count == 1


@pytest.mark.parametrize(
    ("httpx_error", "client_error", "message"),
    [
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
@pytest.mark.parametrize("phase", ["read", "exit"])
def test_read_and_context_exit_errors_close_the_response(
    httpx_error: type[httpx.RequestError],
    client_error: type[GitHubClientError],
    message: str,
    phase: str,
) -> None:
    network_error = httpx_error("RAW_SECRET stream details")
    if phase == "read":
        stream = TrackingStream([b'{"type":', network_error, b"must not be consumed"])
        status = 200
    else:
        # 404 分支不读取正文；错误只能发生在 stream 上下文退出并关闭响应时。
        stream = TrackingStream([b"must not be consumed"], close_error=network_error)
        status = 404
    with mock_file_response(status=status, stream=stream) as (client, upstream):
        with pytest.raises(client_error) as error:
            client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert str(error.value) == message
        assert error.value.__suppress_context__
        assert stream.consumed == (stream.steps[:2] if phase == "read" else [])
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
def test_json_recursion_errors_have_safe_classification(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail_json(_body: str) -> NoReturn:
        raise RecursionError("RAW_SECRET JSON details")

    expected = GitHubUpstreamError if status == 200 else GitHubForbiddenError
    with mock_file_response(b"{}", status=status) as (client, upstream):
        with monkeypatch.context() as patch:
            patch.setattr("app.github_client.json.loads", fail_json)
            with pytest.raises(expected) as error:
                client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
        assert "RAW_SECRET" not in str(error.value)
        assert_response_closed(client, upstream)


def test_identity_header_does_not_change_metadata_requests_and_client_is_reusable() -> None:
    requests: list[httpx.Request] = []
    streams: list[TrackingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/contents/" in request.url.path:
            body = json_body(file_payload())
        else:
            body = json_body(
                {
                    "full_name": "sample-owner/sample-repo",
                    "description": None,
                    "language": "Python",
                    "stargazers_count": 1,
                    "default_branch": "main",
                    "archived": False,
                    "html_url": REPOSITORY_URL,
                }
            )
        stream = TrackingStream([body])
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    transport = TrackingTransport(handler)
    with GitHubRepositoryClient(transport=transport) as client:
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert client.fetch_repository(REPOSITORY_URL).stars == 1
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert not client.is_closed
        assert transport.close_count == 0
        assert len(requests) == 3
        assert requests[0].headers["Accept-Encoding"] == "identity"
        assert requests[1].headers["Accept-Encoding"] != "identity"
        assert requests[2].headers["Accept-Encoding"] == "identity"
        assert all(stream.close_count == 1 for stream in streams)
    assert client.is_closed
    assert transport.close_count == 1
    client.close()
    assert transport.close_count == 1


def test_explicit_close_releases_transport_and_prevents_more_requests() -> None:
    upstream = MockUpstream(TrackingStream([json_body(file_payload())]), 200, {})
    transport = TrackingTransport(upstream.respond)
    client = GitHubRepositoryClient(transport=transport)
    try:
        assert client.fetch_python_file(REPOSITORY_URL, FILE_PATH) == "print('hello')\n"
        assert_response_closed(client, upstream)
    finally:
        client.close()
    assert client.is_closed
    assert transport.close_count == 1
    client.close()
    assert transport.close_count == 1
    with pytest.raises(RuntimeError):
        client.fetch_python_file(REPOSITORY_URL, FILE_PATH)
    assert len(upstream.requests) == 1
