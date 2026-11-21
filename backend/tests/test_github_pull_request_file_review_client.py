"""离线验证 PR 来源解析和按提交读取文件，不执行或保存上游源码。"""

import base64
import json
import socket
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from typing import NoReturn

import httpx
import pytest

from app.github_client import (
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

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
FILE_PATH = "src/example.py"
HEAD_SHA = "a1" * 20
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件读取响应。"
SOURCE_MESSAGE = "PR 来源仓库不可用于公开文件审查。"
MISSING = object()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 文件读取测试禁止真实 DNS 和网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def pull_payload() -> dict[str, object]:
    return {
        "number": 42,
        "title": "修复示例",
        "state": "open",
        "draft": False,
        "merged": False,
        "html_url": f"{REPOSITORY_URL}/pull/42",
        "base": {"ref": "main", "sha": "b2" * 20},
        "head": {
            "ref": "feature/中文分支",
            "sha": HEAD_SHA,
            "repo": {
                "full_name": "contributor/sample-fork",
                "private": False,
                "url": "http://127.0.0.1/private",
                "html_url": "https://evil.invalid/fork",
            },
        },
        "changed_files": 0,
        "additions": 0,
        "deletions": 0,
        "diff_url": "http://127.0.0.1/private",
        "patch_url": "https://evil.invalid/patch",
    }


def json_body(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def file_payload(source: bytes = b"pass\n") -> dict[str, object]:
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(source),
        "path": FILE_PATH,
        "name": "example.py",
        "content": base64.b64encode(source).decode("ascii") + "\n",
        "download_url": "http://127.0.0.1/private",
        "git_url": "https://evil.invalid/blob",
    }


class TrackingStream(httpx.SyncByteStream):
    def __init__(
        self, steps: list[bytes | Exception], close_error: Exception | None = None
    ) -> None:
        self.steps = steps
        self.close_error = close_error
        self.consumed: list[bytes | Exception] = []
        self.close_count = 0

    def __iter__(self) -> Iterator[bytes]:
        for step in self.steps:
            self.consumed.append(step)
            if isinstance(step, Exception):
                raise step
            yield step

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def response(
    payload: object = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    stream: TrackingStream | None = None,
) -> httpx.Response:
    actual_stream = stream if stream is not None else TrackingStream([json_body(payload)])
    return httpx.Response(
        status, headers=headers, stream=actual_stream, extensions={"test_stream": actual_stream}
    )


@contextmanager
def mock_client(
    responses: list[httpx.Response | Exception],
) -> Iterator[tuple[GitHubRepositoryClient, list[httpx.Request]]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(request)
        if index >= len(responses):
            pytest.fail("发生了额外请求、自动重试或默认分支回退")
        item = responses[index]
        if isinstance(item, Exception):
            raise item
        if index and isinstance(responses[index - 1], httpx.Response):
            assert responses[index - 1].is_closed
        return item

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        yield client, requests
    assert client.is_closed


def assert_closed(*responses: httpx.Response) -> None:
    for item in responses:
        assert item.is_closed
        stream = item.extensions["test_stream"]
        assert isinstance(stream, TrackingStream)
        assert stream.close_count == 1


def assert_safe_error(error: BaseException, message: str) -> None:
    assert str(error) == message
    assert "RAW_SECRET" not in "".join(traceback.format_exception(error))


@pytest.mark.parametrize("source_name", ["sample-owner/sample-repo", "Contributor/Sample-Fork"])
def test_two_fixed_requests_pin_source_and_sha(
    pull_payload: dict[str, object], source_name: str
) -> None:
    pull_payload["head"]["repo"]["full_name"] = source_name
    first, second = response(pull_payload), response(file_payload())
    with mock_client([first, second]) as (client, requests):
        result = client.fetch_pull_request_python_file(
            "https://GITHUB.COM/Sample-Owner/Sample-Repo/", 42, FILE_PATH
        )
        assert result == GitHubPullRequestPythonFile(
            f"https://github.com/{source_name}", HEAD_SHA, "pass\n"
        )
        assert len(requests) == 2
        assert (
            str(requests[0].url) == "https://api.github.com/repos/Sample-Owner/Sample-Repo/pulls/42"
        )
        assert requests[1].url.path == f"/repos/{source_name}/contents/{FILE_PATH}"
        assert dict(requests[1].url.params) == {"ref": HEAD_SHA}
        for request in requests:
            assert request.method == "GET"
            assert request.url.scheme == "https"
            assert request.url.host == "api.github.com"
            assert request.headers["Accept-Encoding"] == "identity"
            assert request.headers["Accept"] == "application/vnd.github+json"
            assert request.headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert_closed(first, second)
        assert not client.is_closed


@pytest.mark.parametrize(
    "source", ["", "# 中文\npass\n", "def (", "raise RuntimeError('RAW_SECRET')\n"]
)
def test_source_is_returned_unchanged_without_analysis_or_execution(
    pull_payload: dict[str, object], source: str
) -> None:
    first, second = response(pull_payload), response(file_payload(source.encode("utf-8")))
    with mock_client([first, second]) as (client, requests):
        result = client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert result.code == source
        assert result.head_sha == HEAD_SHA
        assert "RAW_SECRET" not in repr(result)
        assert len(requests) == 2
        assert_closed(first, second)


def test_result_metadata_cannot_be_reassigned() -> None:
    result = GitHubPullRequestPythonFile(REPOSITORY_URL, HEAD_SHA, "secret source")
    with pytest.raises(FrozenInstanceError):
        result.head_sha = "b2" * 20
    assert result.head_sha == HEAD_SHA
    assert "secret source" not in repr(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", "https://github.com.evil.invalid/owner/repo"),
        ("repository_url", None),
        ("pull_number", True),
        ("pull_number", "42"),
        ("pull_number", 42.0),
        ("pull_number", 0),
        ("pull_number", 2_147_483_648),
        ("file_path", "../example.py"),
        ("file_path", "src/示例.py"),
        ("file_path", "src/example.PY"),
        ("file_path", None),
    ],
)
def test_invalid_input_never_requests_upstream(field: str, value: object) -> None:
    args = {"repository_url": REPOSITORY_URL, "pull_number": 42, "file_path": FILE_PATH}
    args[field] = value
    with mock_client([]) as (client, requests):
        with pytest.raises(ValueError) as error:
            client.fetch_pull_request_python_file(**args)
        assert_safe_error(error.value, "仓库地址、PR 编号或 Python 文件路径无效。")
        assert requests == []
        assert not client.is_closed


@pytest.mark.parametrize(
    ("path", "value", "error_type"),
    [
        (("number",), 43, GitHubUpstreamError),
        (("number",), True, GitHubUpstreamError),
        (("html_url",), "https://github.com/other/repo/pull/42", GitHubUpstreamError),
        (("html_url",), "https://github.com/ſample-owner/sample-repo/pull/42", GitHubUpstreamError),
        (("head", "sha"), "a" * 39, GitHubUpstreamError),
        (("head", "sha"), "A" * 40, GitHubUpstreamError),
        (("head", "sha"), "a" * 39 + "\n", GitHubUpstreamError),
        (("head", "repo"), MISSING, GitHubUpstreamError),
        (("head", "repo"), None, GitHubPullRequestSourceUnavailableError),
        (("head", "repo"), [], GitHubUpstreamError),
        (("head", "repo", "full_name"), MISSING, GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/repo/", GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/repo?x=1", GitHubUpstreamError),
        (("head", "repo", "full_name"), "https://evil.invalid/repo", GitHubUpstreamError),
        (("head", "repo", "full_name"), "../repo", GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/仓库", GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/repo.git", GitHubUpstreamError),
        (("head", "repo", "full_name"), None, GitHubUpstreamError),
        (("head", "repo", "private"), MISSING, GitHubUpstreamError),
        (("head", "repo", "private"), "false", GitHubUpstreamError),
        (("head", "repo", "private"), 0, GitHubUpstreamError),
        (("head", "repo", "private"), True, GitHubPullRequestSourceUnavailableError),
    ],
)
def test_bad_identity_or_unavailable_source_stops_before_file_request(
    pull_payload: dict[str, object],
    path: tuple[str, ...],
    value: object,
    error_type: type[Exception],
) -> None:
    item = pull_payload
    for key in path[:-1]:
        item = item[key]
    if value is MISSING:
        del item[path[-1]]
    else:
        item[path[-1]] = value
    first = response(pull_payload)
    message = (
        SOURCE_MESSAGE
        if error_type is GitHubPullRequestSourceUnavailableError
        else UPSTREAM_MESSAGE
    )
    with mock_client([first]) as (client, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert_safe_error(error.value, message)
        assert len(requests) == 1
        assert_closed(first)
        assert not client.is_closed


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
@pytest.mark.parametrize("overflow", [False, True])
def test_stream_limits_and_stop_consuming_after_overflow(
    pull_payload: dict[str, object], stage: int, overflow: bool
) -> None:
    limit = 1_048_576 if stage == 0 else 65_536
    body = json_body(pull_payload if stage == 0 else file_payload())
    bounded = body + b" " * (limit - len(body))
    chunks = [bounded, b" ", b"unread"] if overflow else [bounded]
    stream = TrackingStream(chunks)
    target = response(stream=stream, headers={"Content-Length": "0"})
    prefix = [response(pull_payload)] if stage else []
    suffix = [] if stage or overflow else [response(file_payload())]
    with mock_client([*prefix, target, *suffix]) as (client, requests):
        if overflow:
            with pytest.raises(GitHubUpstreamError) as error:
                client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
            assert_safe_error(error.value, UPSTREAM_MESSAGE)
            assert stream.consumed == chunks[:2]
            assert len(requests) == stage + 1
        else:
            assert (
                client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH).code
                == "pass\n"
            )
            assert stream.consumed == [bounded]
            assert len(requests) == 2
        assert_closed(*prefix, target, *suffix)


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
@pytest.mark.parametrize("body", [b"\xff", b"{", b"[]", b"null", b"[" * 2000 + b"]" * 2000])
def test_both_phases_require_utf8_json_objects(
    pull_payload: dict[str, object], stage: int, body: bytes
) -> None:
    target = response(stream=TrackingStream([body]))
    prefix = [response(pull_payload)] if stage else []
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert len(requests) == stage + 1
        assert_closed(*prefix, target)


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
def test_encoded_bodies_are_rejected_without_reading(
    pull_payload: dict[str, object], stage: int
) -> None:
    stream = TrackingStream([b"unread"])
    target = response(headers={"Content-Encoding": "gzip"}, stream=stream)
    prefix = [response(pull_payload)] if stage else []
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(GitHubUpstreamError):
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert stream.consumed == []
        assert len(requests) == stage + 1
        assert_closed(*prefix, target)


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
@pytest.mark.parametrize(
    ("status", "headers", "body", "error_type", "message", "read_body"),
    [
        (
            404,
            {},
            b"unread",
            GitHubNotFoundError,
            "仓库、PR、提交或文件不存在或不可公开访问。",
            False,
        ),
        (429, {}, b"unread", GitHubRateLimitError, "GitHub 请求受到限流，请稍后重试。", False),
        (
            403,
            {"X-RateLimit-Remaining": " 0 "},
            b"unread",
            GitHubRateLimitError,
            "GitHub 请求受到限流，请稍后重试。",
            False,
        ),
        (
            302,
            {"Location": "http://127.0.0.1/private"},
            b"unread",
            GitHubRedirectError,
            "GitHub 返回了重定向，请使用仓库当前的主页地址。",
            False,
        ),
        (500, {}, b"unread", GitHubUpstreamError, UPSTREAM_MESSAGE, False),
        (
            403,
            {},
            json_body({"message": "API rate limit RAW_SECRET"}),
            GitHubRateLimitError,
            "GitHub 请求受到限流，请稍后重试。",
            True,
        ),
        (
            403,
            {},
            json_body({"message": "secondary rate limit RAW_SECRET"}),
            GitHubRateLimitError,
            "GitHub 请求受到限流，请稍后重试。",
            True,
        ),
        (
            403,
            {},
            json_body({"message": "ordinary RAW_SECRET"}),
            GitHubForbiddenError,
            "GitHub 拒绝访问该仓库、PR 或文件。",
            True,
        ),
        (403, {}, b"invalid RAW_SECRET", GitHubUpstreamError, UPSTREAM_MESSAGE, True),
    ],
)
def test_status_errors_are_bounded_sanitized_and_not_retried(
    pull_payload: dict[str, object],
    stage: int,
    status: int,
    headers: dict[str, str],
    body: bytes,
    error_type: type[Exception],
    message: str,
    read_body: bool,
) -> None:
    stream = TrackingStream([body])
    target = response(status=status, headers=headers, stream=stream)
    prefix = [response(pull_payload)] if stage else []
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert_safe_error(error.value, message)
        assert len(requests) == stage + 1
        assert stream.consumed == ([body] if read_body else [])
        assert_closed(*prefix, target)


@pytest.mark.parametrize(
    ("patch", "error_type"),
    [
        ({"type": "dir"}, GitHubUnsupportedFileError),
        ({"type": "symlink"}, GitHubUnsupportedFileError),
        ({"encoding": "none"}, GitHubUnsupportedFileError),
        ({"target": None}, GitHubUnsupportedFileError),
        ({"submodule_git_url": "https://evil.invalid/repo"}, GitHubUnsupportedFileError),
        ({"path": "other.py"}, GitHubUpstreamError),
        ({"name": "other.py"}, GitHubUpstreamError),
        ({"size": True}, GitHubUpstreamError),
        ({"size": 1}, GitHubUpstreamError),
        ({"size": 20_001}, GitHubFileTooLargeError),
        ({"content": "A" * 26_672}, GitHubFileTooLargeError),
        ({"content": "cGFzcw"}, GitHubUpstreamError),
        ({"content": "cG Fzcwo="}, GitHubUpstreamError),
        ({"content": "\tcGFzcwo="}, GitHubUpstreamError),
        ({"content": "/w==", "size": 1}, GitHubUnsupportedFileError),
        (
            {"content": base64.b64encode(b"x" * 20_001).decode("ascii"), "size": 0},
            GitHubFileTooLargeError,
        ),
    ],
)
def test_file_validation_reuses_existing_limits_and_decoding(
    pull_payload: dict[str, object], patch: dict[str, object], error_type: type[Exception]
) -> None:
    data = file_payload()
    data.update(patch)
    first, second = response(pull_payload), response(data)
    with mock_client([first, second]) as (client, requests):
        with pytest.raises(error_type):
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert len(requests) == 2
        assert_closed(first, second)


def test_source_byte_limit_and_crlf_base64(pull_payload: dict[str, object]) -> None:
    source = "中" * 6666 + "\n\n"
    assert len(source.encode("utf-8")) == 20_000
    data = file_payload(source.encode("utf-8"))
    encoded = data["content"]
    data["content"] = "\r\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
    first, second = response(pull_payload), response(data)
    with mock_client([first, second]) as (client, requests):
        assert client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH).code == source
        assert len(requests) == 2
        assert_closed(first, second)


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
@pytest.mark.parametrize("fault", ["connect", "read", "close"])
@pytest.mark.parametrize(
    ("httpx_type", "error_type", "message"),
    [
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ConnectError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_network_errors_cover_both_phases_and_context_exit(
    pull_payload: dict[str, object],
    stage: int,
    fault: str,
    httpx_type: type[httpx.RequestError],
    error_type: type[Exception],
    message: str,
) -> None:
    failure = httpx_type("RAW_SECRET")
    prefix = [response(pull_payload)] if stage else []
    if fault == "connect":
        target = failure
    else:
        body = json_body(pull_payload if stage == 0 else file_payload())
        stream = TrackingStream(
            [failure] if fault == "read" else [body], failure if fault == "close" else None
        )
        target = response(stream=stream)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert_safe_error(error.value, message)
        assert len(requests) == stage + 1
        assert_closed(*prefix)
        if isinstance(target, httpx.Response):
            assert_closed(target)
        assert not client.is_closed


@pytest.mark.parametrize("stage", [0, 1], ids=["pr", "file"])
def test_client_is_reusable_after_failed_download(
    pull_payload: dict[str, object], stage: int
) -> None:
    prefix = [response(pull_payload)] if stage else []
    failed = response(status=404)
    first, second = response(pull_payload), response(file_payload())
    with mock_client([*prefix, failed, first, second]) as (client, requests):
        with pytest.raises(GitHubNotFoundError):
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert len(requests) == stage + 1
        assert not client.is_closed
        assert client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH).code == "pass\n"
        assert len(requests) == stage + 3
        assert_closed(*prefix, failed, first, second)


def test_unexpected_program_error_is_not_disguised(pull_payload: dict[str, object]) -> None:
    first = response(pull_payload)
    second = response(stream=TrackingStream([RuntimeError("unexpected internal failure")]))
    with mock_client([first, second]) as (client, requests):
        with pytest.raises(RuntimeError, match="unexpected internal failure"):
            client.fetch_pull_request_python_file(REPOSITORY_URL, 42, FILE_PATH)
        assert len(requests) == 2
        assert_closed(first, second)
