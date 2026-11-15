"""用 MockTransport 和未预读的流验证单页 PR 文件列表，不访问真实网络。"""

import json
import socket
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
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
from app.schemas import GitHubPullRequestFilesResponse

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
BODY_LIMIT = 1_048_576
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件列表响应。"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 文件列表测试禁止真实 DNS 查询或网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def file_payload() -> dict[str, object]:
    return {
        "filename": "文档/新 文件.md",
        "status": "renamed",
        "additions": 4,
        "deletions": 2,
        "changes": 6,
        "previous_filename": "文档/旧 文件.md",
    }


def json_body(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


class TrackingStream(httpx.SyncByteStream):
    """记录块消费和关闭，便于验证超限后不再读取。"""

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


@contextmanager
def mock_response(
    body: bytes = b"",
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    stream: TrackingStream | None = None,
) -> Iterator[tuple[GitHubRepositoryClient, httpx.Response, TrackingStream, list[httpx.Request]]]:
    actual_stream = stream if stream is not None else TrackingStream([body])
    response = httpx.Response(status, headers=headers, stream=actual_stream)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        yield client, response, actual_stream, requests
    assert client.is_closed


def assert_response_closed(
    client: GitHubRepositoryClient, response: httpx.Response, stream: TrackingStream
) -> None:
    assert response.is_closed
    assert stream.close_count == 1
    assert not client.is_closed


def assert_safe_error(error: BaseException, message: str) -> None:
    assert str(error) == message
    assert "RAW_SECRET" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("page", [1, 2, 100])
def test_fixed_single_request_and_explicit_metadata_mapping(
    file_payload: dict[str, object], page: int
) -> None:
    file_payload.update(
        patch="RAW_SECRET",
        raw_url="http://127.0.0.1/private",
        contents_url="https://evil.invalid/file",
        blob_url="https://evil.invalid/blob",
        sha="a" * 40,
    )
    repository_url = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
    with mock_response(
        json_body([file_payload]),
        headers={"Link": '<https://evil.invalid/next>; rel="next"'},
    ) as (client, response, stream, requests):
        result = client.fetch_pull_request_files(repository_url, 42, page)
        assert isinstance(result, GitHubPullRequestFilesResponse)
        assert result.model_dump() == {
            "repository_url": repository_url,
            "pull_number": 42,
            "page": page,
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
        assert len(requests) == 1
        request = requests[0]
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api.github.com"
        assert request.url.port in {None, 443}
        assert request.url.path == "/repos/Sample-Owner/Sample-Repo/pulls/42/files"
        assert sorted(request.url.params.multi_items()) == [("page", str(page)), ("per_page", "30")]
        assert request.content == b""
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["User-Agent"] == "RepoPilot"
        assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        assert_response_closed(client, response, stream)


def test_default_page_and_empty_page_are_valid() -> None:
    with mock_response(b"[]") as (client, response, stream, requests):
        result = client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert (result.page, result.per_page, result.files) == (1, 30, [])
        assert requests[0].url.params["page"] == "1"
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository_url", None),
        ("repository_url", b"https://github.com/a/b"),
        ("repository_url", "http://github.com/a/b"),
        ("repository_url", "https://github.com.evil.invalid/a/b"),
        ("repository_url", "https://secret@github.com/a/b"),
        ("repository_url", "https://github.com:443/a/b"),
        ("repository_url", "https://github.com/a/b?x=RAW_SECRET"),
        ("repository_url", "https://github.com/a/b#fragment"),
        ("repository_url", "https://github.com/a/b%2Ffiles"),
        ("repository_url", "https://github.com/a/b\\files"),
        ("repository_url", "https://github.com/a/b\n"),
        ("repository_url", "https://github.com/a/b/pull/42"),
        ("repository_url", "https://github.com/a/b.git"),
        ("pull_number", 0),
        ("pull_number", 2_147_483_648),
        ("pull_number", True),
        ("pull_number", False),
        ("pull_number", "42"),
        ("pull_number", 42.0),
        ("pull_number", None),
        ("page", 0),
        ("page", 101),
        ("page", True),
        ("page", False),
        ("page", "1"),
        ("page", 1.0),
        ("page", None),
    ],
)
def test_invalid_inputs_fail_before_request(field: str, value: object) -> None:
    arguments = {"repository_url": REPOSITORY_URL, "pull_number": 42, "page": 1}
    arguments[field] = value
    with mock_response() as (client, response, stream, requests):
        with pytest.raises(ValueError) as error:
            client.fetch_pull_request_files(**arguments)
        assert_safe_error(error.value, "仓库地址、PR 编号或页码无效。")
        assert requests == []
        assert stream.consumed == []
        assert stream.close_count == 0
        assert not response.is_closed


@pytest.mark.parametrize("count", [0, 30, 31])
def test_page_length_and_order(file_payload: dict[str, object], count: int) -> None:
    payload = [dict(file_payload, filename=f"文件 {i}.txt") for i in range(count)]
    with mock_response(json_body(payload)) as (client, response, stream, _requests):
        if count == 31:
            with pytest.raises(GitHubUpstreamError) as error:
                client.fetch_pull_request_files(REPOSITORY_URL, 42, 100)
            assert_safe_error(error.value, UPSTREAM_MESSAGE)
        else:
            result = client.fetch_pull_request_files(REPOSITORY_URL, 42, 100)
            assert result.page == 100
            assert [file.filename for file in result.files] == [
                f"文件 {i}.txt" for i in range(count)
            ]
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("field", ["filename", "status", "additions", "deletions", "changes"])
def test_missing_required_file_fields(file_payload: dict[str, object], field: str) -> None:
    del file_payload[field]
    with mock_response(json_body([file_payload])) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "field,value",
    [
        ("filename", ""),
        ("filename", None),
        ("filename", 1),
        ("previous_filename", ""),
        ("previous_filename", False),
        ("status", "deleted"),
        ("status", "Modified"),
        ("status", None),
        ("status", True),
        ("additions", -1),
        ("deletions", -1),
        ("changes", -1),
        ("additions", True),
        ("deletions", 1.0),
        ("changes", "1"),
        ("additions", None),
    ],
)
def test_invalid_metadata_is_sanitized(
    file_payload: dict[str, object], field: str, value: object
) -> None:
    file_payload[field] = value
    file_payload["patch"] = "RAW_SECRET"
    with mock_response(json_body([file_payload])) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "status", ["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
)
def test_statuses_and_optional_previous_filename(
    file_payload: dict[str, object], status: str
) -> None:
    del file_payload["previous_filename"]
    file_payload.update(status=status, additions=1, deletions=2, changes=0)
    with mock_response(json_body([file_payload])) as (client, response, stream, _requests):
        result = client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert result.files[0].status == status
        assert result.files[0].previous_filename is None
        assert result.files[0].changes == 0
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("field", ["filename", "previous_filename"])
@pytest.mark.parametrize("value", ["README.md", "目录/空 格.png", " a ", "../metadata.py"])
def test_file_names_remain_metadata(
    file_payload: dict[str, object], field: str, value: str
) -> None:
    file_payload[field] = value
    with mock_response(json_body([file_payload])) as (client, response, stream, requests):
        result = client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert result.files[0].model_dump()[field] == value
        assert len(requests) == 1
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("field", ["filename", "previous_filename"])
@pytest.mark.parametrize("value", ["\ud800", "\udfff"])
def test_surrogates_are_rejected_before_response_serialization(
    file_payload: dict[str, object], field: str, value: str
) -> None:
    file_payload[field] = "RAW_SECRET" + value
    with mock_response(json_body([file_payload])) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"{",
        b"[] RAW_SECRET",
        b'"\xff"',
        b"\xef\xbb\xbf[]",
        b"\xff\xfe[\x00]\x00",
        b"null",
        b"{}",
        b"42",
        b'"RAW_SECRET"',
        b"[null]",
        b"[[]]",
        b"[" * 1100 + b"]" * 1100,
    ],
)
def test_invalid_utf8_json_or_shape_is_sanitized(body: bytes) -> None:
    with mock_response(body) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("size", [BODY_LIMIT - 1, BODY_LIMIT])
@pytest.mark.parametrize("content_length", [None, "1", "9999999", "invalid", "-1"])
def test_body_limit_uses_received_bytes(size: int, content_length: str | None) -> None:
    body = b"[]" + b" " * (size - 2)
    stream = TrackingStream([body[:100], body[100:]])
    headers = {} if content_length is None else {"Content-Length": content_length}
    with mock_response(headers=headers, stream=stream) as (client, response, actual, _requests):
        assert client.fetch_pull_request_files(REPOSITORY_URL, 42).files == []
        assert stream.consumed == stream.steps
        assert_response_closed(client, response, actual)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("single_chunk", [True, False])
def test_overflow_stops_reading_and_closes(status: int, single_chunk: bool) -> None:
    prefix = [b"x" * (BODY_LIMIT + 1)] if single_chunk else [b"x" * BODY_LIMIT, b"x"]
    stream = TrackingStream([*prefix, httpx.ReadError("RAW_SECRET must not be consumed")])
    with mock_response(status=status, stream=stream, headers={"Content-Length": "1"}) as (
        client,
        response,
        actual,
        _requests,
    ):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert stream.consumed == prefix
        assert_response_closed(client, response, actual)


def test_discarded_patch_still_counts_toward_body_limit(file_payload: dict[str, object]) -> None:
    file_payload["patch"] = "x" * BODY_LIMIT
    with mock_response(json_body([file_payload])) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("offset", [1, 2])
def test_multibyte_utf8_can_cross_chunk_boundaries(
    file_payload: dict[str, object], offset: int
) -> None:
    body = json.dumps([file_payload], ensure_ascii=False).encode("utf-8")
    split = body.index("文".encode("utf-8")) + offset
    stream = TrackingStream([body[:split], body[split:]])
    with mock_response(stream=stream) as (client, response, actual, _requests):
        assert (
            client.fetch_pull_request_files(REPOSITORY_URL, 42).files[0].filename
            == file_payload["filename"]
        )
        assert_response_closed(client, response, actual)


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", ""])
@pytest.mark.parametrize("status", [200, 403])
def test_compressed_responses_are_rejected_without_reading(status: int, encoding: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be consumed")])
    with mock_response(status=status, stream=stream, headers={"Content-Encoding": encoding}) as (
        client,
        response,
        actual,
        _requests,
    ):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert stream.consumed == []
        assert_response_closed(client, response, actual)


@pytest.mark.parametrize("encoding", ["identity", " Identity "])
def test_identity_encoding_is_accepted(encoding: str) -> None:
    with mock_response(b"[]", headers={"Content-Encoding": encoding}) as (
        client,
        response,
        stream,
        _requests,
    ):
        assert client.fetch_pull_request_files(REPOSITORY_URL, 42).files == []
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "status,error_type,message",
    [
        (404, GitHubNotFoundError, "仓库或 PR 不存在或不可公开访问。"),
        (429, GitHubRateLimitError, "GitHub 请求受到限流，请稍后重试。"),
        *[
            (status, GitHubRedirectError, "GitHub 返回了重定向，请使用仓库当前的主页地址。")
            for status in (300, 301, 302, 303, 304, 307, 308, 399)
        ],
        *[
            (status, GitHubUpstreamError, UPSTREAM_MESSAGE)
            for status in (201, 204, 400, 401, 422, 500, 503)
        ],
    ],
)
def test_status_errors_skip_body_and_never_follow_redirects(
    status: int, error_type: type[GitHubClientError], message: str
) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be consumed")])
    with mock_response(
        status=status,
        stream=stream,
        headers={"Content-Encoding": "gzip", "Location": "http://127.0.0.1/private"},
    ) as (client, response, actual, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, message)
        assert len(requests) == 1
        assert stream.consumed == []
        assert_response_closed(client, response, actual)


@pytest.mark.parametrize("remaining", ["0", " 0 "])
def test_403_zero_remaining_skips_body(remaining: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be consumed")])
    with mock_response(
        status=403,
        stream=stream,
        headers={"X-RateLimit-Remaining": remaining, "Content-Encoding": "gzip"},
    ) as (client, response, actual, _requests):
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, "GitHub 请求受到限流，请稍后重试。")
        assert stream.consumed == []
        assert_response_closed(client, response, actual)


@pytest.mark.parametrize(
    "message", ["API RATE LIMIT exceeded RAW_SECRET", "Secondary rate limit RAW_SECRET"]
)
def test_403_rate_limit_message(message: str) -> None:
    with mock_response(json_body({"message": message}), status=403) as (
        client,
        response,
        stream,
        _requests,
    ):
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, "GitHub 请求受到限流，请稍后重试。")
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("payload", [{}, {"message": "RAW_SECRET denied"}, {"message": None}])
def test_403_without_rate_limit_evidence_is_forbidden(payload: object) -> None:
    with mock_response(json_body(payload), status=403) as (client, response, stream, _requests):
        with pytest.raises(GitHubForbiddenError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, "GitHub 拒绝访问该仓库或 PR。")
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize("body", [b"RAW_SECRET", b"[]", b"null"])
def test_invalid_403_body_is_upstream_error(body: bytes) -> None:
    with mock_response(body, status=403) as (client, response, stream, _requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, response, stream)


@pytest.mark.parametrize(
    "httpx_error,error_type,message",
    [
        (httpx.ConnectTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.WriteTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.PoolTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ConnectError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.ReadError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RemoteProtocolError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_connection_errors_are_sanitized(
    httpx_error: type[httpx.RequestError], error_type: type[GitHubClientError], message: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx_error("RAW_SECRET")

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, message)
        assert len(requests) == 1
        assert not client.is_closed
    assert client.is_closed


@pytest.mark.parametrize("phase", ["read", "close-success", "close-status-error"])
@pytest.mark.parametrize(
    "httpx_error,error_type,message",
    [
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RemoteProtocolError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_read_and_context_exit_errors_are_sanitized(
    phase: str,
    httpx_error: type[httpx.RequestError],
    error_type: type[GitHubClientError],
    message: str,
) -> None:
    failure = httpx_error("RAW_SECRET")
    if phase == "read":
        stream = TrackingStream([b"[", failure, b"must not be consumed"])
        expected = stream.steps[:2]
    else:
        stream = TrackingStream([b"[]"], close_error=failure)
        expected = [] if phase == "close-status-error" else stream.steps
    with mock_response(status=404 if phase == "close-status-error" else 200, stream=stream) as (
        client,
        response,
        actual,
        _requests,
    ):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert_safe_error(error.value, message)
        assert stream.consumed == expected
        assert_response_closed(client, response, actual)


def test_client_can_be_reused_after_success_and_failure() -> None:
    streams: list[TrackingStream] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        stream = TrackingStream([b"[]"])
        streams.append(stream)
        return httpx.Response(500 if len(requests) == 2 else 200, stream=stream)

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        assert client.fetch_pull_request_files(REPOSITORY_URL, 42).files == []
        with pytest.raises(GitHubUpstreamError):
            client.fetch_pull_request_files(REPOSITORY_URL, 42)
        assert client.fetch_pull_request_files(REPOSITORY_URL, 42, 2).page == 2
        assert len(requests) == 3
        assert all(stream.close_count == 1 for stream in streams)
        assert not client.is_closed
    assert client.is_closed
