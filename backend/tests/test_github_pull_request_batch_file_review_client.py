"""用 MockTransport 验证受限批量读取，独立阻断真实 DNS 和网络连接。"""

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

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
FILE_PATHS = ["src/z.py", "src/a.py", "Src/B.py"]
HEAD_SHA = "a1" * 20
INPUT_MESSAGE = "仓库地址、PR 编号或 Python 文件路径列表无效。"
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件读取响应。"
SOURCE_MESSAGE = "PR 来源仓库不可用于公开文件审查。"
MISSING = object()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("批量文件客户端测试禁止真实 DNS 和网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def pull_payload() -> dict[str, object]:
    return {
        "number": 42,
        "title": "批量读取示例",
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


def file_payload(file_path: str, source: bytes = b"pass\n") -> dict[str, object]:
    return {
        "type": "file",
        "encoding": "base64",
        "size": len(source),
        "path": file_path,
        "name": file_path.rsplit("/", 1)[-1],
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
            pytest.fail("失败后继续请求、重复查询 PR 或发生额外下载")
        for previous in responses[:index]:
            if isinstance(previous, httpx.Response):
                assert previous.is_closed
        item = responses[index]
        if isinstance(item, Exception):
            raise item
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


def prefix_responses(pull_payload: dict[str, object], stage: int) -> list[httpx.Response]:
    """stage 0 为 PR，1–3 为相应文件；只构造失败点之前的成功响应。"""
    if stage == 0:
        return []
    return [response(pull_payload), *[response(file_payload(p)) for p in FILE_PATHS[: stage - 1]]]


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.parametrize("source_name", ["sample-owner/sample-repo", "Contributor/Sample-Fork"])
def test_one_pr_query_and_ordered_files_share_one_source_and_sha(
    pull_payload: dict[str, object], count: int, source_name: str
) -> None:
    pull_payload["head"]["repo"]["full_name"] = source_name
    paths = FILE_PATHS[:count]
    sources = ["", "# 中文\n", 'raise RuntimeError("MUST_NOT_EXECUTE")\ndef broken(:\n'][:count]
    responses = [
        response(pull_payload),
        *[response(file_payload(p, code.encode("utf-8"))) for p, code in zip(paths, sources)],
    ]
    with mock_client(responses) as (client, requests):
        result = client.fetch_pull_request_python_files(
            "https://GITHUB.COM/Sample-Owner/Sample-Repo/", 42, paths
        )
        assert result == GitHubPullRequestPythonFiles(
            f"https://github.com/{source_name}",
            HEAD_SHA,
            tuple(GitHubPullRequestPythonFileContent(p, code) for p, code in zip(paths, sources)),
        )
        assert isinstance(result.files, tuple)
        assert len(requests) == count + 1
        assert str(requests[0].url) == (
            "https://api.github.com/repos/Sample-Owner/Sample-Repo/pulls/42"
        )
        for request, path in zip(requests[1:], paths):
            assert str(request.url) == (
                f"https://api.github.com/repos/{source_name}/contents/{path}?ref={HEAD_SHA}"
            )
        assert all(request.method == "GET" for request in requests)
        assert all(request.headers["Accept-Encoding"] == "identity" for request in requests)
        assert_closed(*responses)
        assert not client.is_closed


def test_batch_result_is_immutable_and_repr_hides_all_source() -> None:
    item = GitHubPullRequestPythonFileContent("a.py", "secret source")
    result = GitHubPullRequestPythonFiles(REPOSITORY_URL, HEAD_SHA, (item,))
    with pytest.raises(FrozenInstanceError):
        result.head_sha = "b2" * 20
    with pytest.raises(FrozenInstanceError):
        item.code = "changed"
    with pytest.raises(TypeError):
        result.files[0] = item
    assert result.files == (item,)
    assert item.code == "secret source"
    assert "secret source" not in repr(item)
    assert "secret source" not in repr(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository_url", None),
        ("repository_url", b"https://github.com/o/r"),
        ("repository_url", "http://github.com/o/r"),
        ("repository_url", "https://api.github.com/repos/o/r"),
        ("repository_url", "https://github.com.evil.invalid/o/r"),
        ("repository_url", "https://github.com/o/re%70o"),
        ("pull_number", True),
        ("pull_number", "42"),
        ("pull_number", 42.0),
        ("pull_number", 0),
        ("pull_number", 2_147_483_648),
        ("file_paths", None),
        ("file_paths", "a.py"),
        ("file_paths", ("a.py",)),
        ("file_paths", []),
        ("file_paths", ["a.py", "b.py", "c.py", "d.py"]),
        ("file_paths", ["a.py", "a.py"]),
        ("file_paths", ["../bad.py", "b.py", "c.py"]),
        ("file_paths", ["a.py", "src/示例.py", "c.py"]),
        ("file_paths", ["a.py", "b.py", "c.PY"]),
        ("file_paths", ["a.py", None]),
        ("file_paths", ["a.py", []]),
    ],
)
def test_every_input_is_validated_before_any_network_request(field: str, value: object) -> None:
    args = {"repository_url": REPOSITORY_URL, "pull_number": 42, "file_paths": FILE_PATHS.copy()}
    args[field] = value
    with mock_client([]) as (client, requests):
        with pytest.raises(ValueError) as error:
            client.fetch_pull_request_python_files(**args)
        assert_safe_error(error.value, INPUT_MESSAGE)
        assert requests == []
        assert not client.is_closed


@pytest.mark.parametrize(
    ("path", "value", "error_type"),
    [
        (("number",), 43, GitHubUpstreamError),
        (("number",), True, GitHubUpstreamError),
        (("html_url",), "https://github.com/other/repo/pull/42", GitHubUpstreamError),
        (("html_url",), "https://github.com/ſample-owner/sample-repo/pull/42", GitHubUpstreamError),
        (("head", "sha"), "A" * 40, GitHubUpstreamError),
        (("head", "sha"), "a" * 39 + "\n", GitHubUpstreamError),
        (("head", "repo"), MISSING, GitHubUpstreamError),
        (("head", "repo"), None, GitHubPullRequestSourceUnavailableError),
        (("head", "repo"), [], GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/repo/", GitHubUpstreamError),
        (("head", "repo", "full_name"), "https://evil.invalid/repo", GitHubUpstreamError),
        (("head", "repo", "full_name"), "owner/仓库", GitHubUpstreamError),
        (("head", "repo", "private"), MISSING, GitHubUpstreamError),
        (("head", "repo", "private"), "false", GitHubUpstreamError),
        (("head", "repo", "private"), 0, GitHubUpstreamError),
        (("head", "repo", "private"), True, GitHubPullRequestSourceUnavailableError),
    ],
)
def test_invalid_identity_or_source_stops_after_the_pr_response(
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
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert_safe_error(error.value, message)
        assert len(requests) == 1
        assert_closed(first)


@pytest.mark.parametrize("stage", [0, 1, 2, 3], ids=["pr", "first", "second", "third"])
@pytest.mark.parametrize("overflow", [False, True])
def test_each_stream_is_bounded_and_overflow_aborts_the_batch(
    pull_payload: dict[str, object], stage: int, overflow: bool
) -> None:
    limit = 1_048_576 if stage == 0 else 65_536
    payload = pull_payload if stage == 0 else file_payload(FILE_PATHS[stage - 1])
    body = json_body(payload)
    bounded = body + b" " * (limit - len(body))
    chunks = [bounded, b" ", b"unread"] if overflow else [bounded]
    stream = TrackingStream(chunks)
    target = response(headers={"Content-Length": "0"}, stream=stream)
    prefix = prefix_responses(pull_payload, stage)
    suffix = [] if overflow else [response(file_payload(p)) for p in FILE_PATHS[stage:]]
    with mock_client([*prefix, target, *suffix]) as (client, requests):
        if overflow:
            with pytest.raises(GitHubUpstreamError) as error:
                client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
            assert_safe_error(error.value, UPSTREAM_MESSAGE)
            assert stream.consumed == chunks[:2]
            assert len(requests) == stage + 1
        else:
            result = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
            assert [item.code for item in result.files] == ["pass\n"] * 3
            assert len(requests) == 4
            assert stream.consumed == [bounded]
        assert_closed(*prefix, target, *suffix)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize("body", [b"\xff", b"{", b"[]", b"null"])
def test_malformed_utf8_or_json_never_produces_partial_results(
    pull_payload: dict[str, object], stage: int, body: bytes
) -> None:
    target = response(stream=TrackingStream([body]))
    prefix = prefix_responses(pull_payload, stage)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert len(requests) == stage + 1
        assert_closed(*prefix, target)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
def test_encoded_response_is_rejected_before_consuming_body(
    pull_payload: dict[str, object], stage: int
) -> None:
    stream = TrackingStream([b"unread"])
    target = response(headers={"Content-Encoding": "gzip"}, stream=stream)
    prefix = prefix_responses(pull_payload, stage)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(GitHubUpstreamError):
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert stream.consumed == []
        assert len(requests) == stage + 1
        assert_closed(*prefix, target)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
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
def test_every_stage_preserves_error_class_and_stops_following_requests(
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
    prefix = prefix_responses(pull_payload, stage)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert_safe_error(error.value, message)
        assert len(requests) == stage + 1
        assert stream.consumed == ([body] if read_body else [])
        assert_closed(*prefix, target)
        assert not client.is_closed


@pytest.mark.parametrize(
    ("patch", "error_type"),
    [
        ({"type": "symlink"}, GitHubUnsupportedFileError),
        ({"type": "dir"}, GitHubUnsupportedFileError),
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
        ({"content": "/w==", "size": 1}, GitHubUnsupportedFileError),
        (
            {"content": base64.b64encode(b"x" * 20_001).decode("ascii"), "size": 0},
            GitHubFileTooLargeError,
        ),
    ],
)
def test_decode_failure_in_second_file_closes_responses_and_skips_third(
    pull_payload: dict[str, object], patch: dict[str, object], error_type: type[Exception]
) -> None:
    data = file_payload(FILE_PATHS[1])
    data.update(patch)
    prefix = prefix_responses(pull_payload, 2)
    target = response(data)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(error_type):
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert len(requests) == 3
        assert_closed(*prefix, target)
        assert not client.is_closed


def test_three_files_at_byte_limit_allow_sixty_thousand_bytes(
    pull_payload: dict[str, object],
) -> None:
    source = "中" * 6666 + "\n\n"
    assert len(source.encode("utf-8")) == 20_000
    files = []
    for path in FILE_PATHS:
        payload = file_payload(path, source.encode("utf-8"))
        encoded = payload["content"]
        payload["content"] = "\r\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
        files.append(response(payload))
    first = response(pull_payload)
    with mock_client([first, *files]) as (client, requests):
        result = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert sum(len(item.code.encode("utf-8")) for item in result.files) == 60_000
        assert all(item.code == source for item in result.files)
        assert len(requests) == 4
        assert_closed(first, *files)


@pytest.mark.parametrize("stage", [0, 1, 2, 3])
@pytest.mark.parametrize("fault", ["connect", "read", "close"])
@pytest.mark.parametrize(
    ("httpx_type", "error_type", "message"),
    [
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ConnectError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_network_faults_at_each_stage_release_responses_and_abort(
    pull_payload: dict[str, object],
    stage: int,
    fault: str,
    httpx_type: type[httpx.RequestError],
    error_type: type[Exception],
    message: str,
) -> None:
    failure = httpx_type("RAW_SECRET")
    prefix = prefix_responses(pull_payload, stage)
    if fault == "connect":
        target = failure
    else:
        payload = pull_payload if stage == 0 else file_payload(FILE_PATHS[stage - 1])
        stream = TrackingStream(
            [failure] if fault == "read" else [json_body(payload)],
            failure if fault == "close" else None,
        )
        target = response(stream=stream)
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert_safe_error(error.value, message)
        assert len(requests) == stage + 1
        assert_closed(*prefix)
        if isinstance(target, httpx.Response):
            assert_closed(target)
        assert not client.is_closed


def test_client_can_start_another_batch_after_a_failed_middle_file(
    pull_payload: dict[str, object],
) -> None:
    prefix = prefix_responses(pull_payload, 2)
    failed = response(status=404)
    succeeding = [response(pull_payload), response(file_payload(FILE_PATHS[0]))]
    with mock_client([*prefix, failed, *succeeding]) as (client, requests):
        with pytest.raises(GitHubNotFoundError):
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert len(requests) == 3
        assert not client.is_closed
        result = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS[:1])
        assert [item.code for item in result.files] == ["pass\n"]
        assert len(requests) == 5
        assert_closed(*prefix, failed, *succeeding)


def test_source_is_pinned_per_batch_but_not_cached_across_batches(
    pull_payload: dict[str, object],
) -> None:
    first_pr = response(pull_payload)
    pull_payload["head"]["sha"] = "b2" * 20
    responses = [
        first_pr,
        response(file_payload(FILE_PATHS[0])),
        response(file_payload(FILE_PATHS[1])),
        response(pull_payload),
        response(file_payload(FILE_PATHS[0])),
        response(file_payload(FILE_PATHS[1])),
    ]
    with mock_client(responses) as (client, requests):
        first = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS[:2])
        second = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS[:2])
        assert first.head_sha == HEAD_SHA
        assert second.head_sha == "b2" * 20
        assert len(requests) == 6
        assert [r.url.params["ref"] for r in requests[1:3]] == [HEAD_SHA, HEAD_SHA]
        assert [r.url.params["ref"] for r in requests[4:6]] == ["b2" * 20, "b2" * 20]
        assert (
            requests[0].url.path
            == requests[3].url.path
            == "/repos/sample-owner/sample-repo/pulls/42"
        )
        assert_closed(*responses)


def test_network_callback_cannot_change_validated_caller_path_snapshot(
    pull_payload: dict[str, object],
) -> None:
    paths = FILE_PATHS[:2]
    expected = paths.copy()
    responses = [response(pull_payload), *[response(file_payload(p)) for p in expected]]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        index = len(requests)
        requests.append(request)
        if index == 0:
            paths[:] = ["unexpected.py"]
        else:
            assert responses[index - 1].is_closed
        if index >= len(responses):
            pytest.fail("调用方列表改变导致额外读取")
        return responses[index]

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        result = client.fetch_pull_request_python_files(REPOSITORY_URL, 42, paths)
        assert [item.file_path for item in result.files] == expected
        assert [r.url.path.rsplit("/contents/", 1)[-1] for r in requests[1:]] == expected
        assert paths == ["unexpected.py"]
        assert_closed(*responses)
    assert client.is_closed


def test_unexpected_internal_exception_is_not_converted_to_upstream_error(
    pull_payload: dict[str, object],
) -> None:
    prefix = prefix_responses(pull_payload, 2)
    target = response(stream=TrackingStream([RuntimeError("unexpected internal failure")]))
    with mock_client([*prefix, target]) as (client, requests):
        with pytest.raises(RuntimeError, match="unexpected internal failure"):
            client.fetch_pull_request_python_files(REPOSITORY_URL, 42, FILE_PATHS)
        assert len(requests) == 3
        assert_closed(*prefix, target)
