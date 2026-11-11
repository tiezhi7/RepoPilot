"""使用真实客户端、MockTransport 和未预读的流离线验证 PR 概览。"""

import base64
import json
import socket
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
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
from app.schemas import GitHubPullRequestResponse

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
PULL_NUMBER = 42
BODY_LIMIT = 1_048_576
UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 响应。"
UPSTREAM_FIELDS = (
    "number",
    "title",
    "state",
    "draft",
    "merged",
    "html_url",
    "base",
    "head",
    "changed_files",
    "additions",
    "deletions",
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """本测试文件独立阻断 DNS 和 socket，不能退回真实网络。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 客户端测试禁止真实 DNS 查询和网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def pull_payload() -> dict[str, object]:
    """编号和网页地址对应，不依赖任何真实 PR。"""
    return {
        "number": PULL_NUMBER,
        "title": "修复重试 🙂",
        "state": "open",
        "draft": False,
        "merged": False,
        "html_url": f"{REPOSITORY_URL}/pull/{PULL_NUMBER}",
        "base": {"ref": "发布/版本一", "sha": "a1" * 20},
        "head": {"ref": "feature/重试🚀", "sha": "b2" * 20, "repo": None},
        "changed_files": 2,
        "additions": 10,
        "deletions": 3,
    }


def json_body(payload: object) -> bytes:
    # 显式序列化 None，确保它表示 JSON null，而非空响应体。
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class TrackingStream(httpx.SyncByteStream):
    """记录实际消费的块和关闭次数，支持读取及关闭阶段的 HTTPX 错误。"""

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
def mock_pull_response(
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


def assert_safe_error(error: BaseException, message: str) -> None:
    assert str(error) == message
    assert "RAW_SECRET" not in repr(error)
    assert "RAW_SECRET" not in "".join(traceback.format_exception(error))


def set_upstream_field(payload: dict[str, object], path: str, value: object) -> None:
    if "." not in path:
        payload[path] = value
        return
    side, key = path.split(".")
    nested = payload[side]
    if not isinstance(nested, dict):
        pytest.fail("测试样本的 base/head 必须先是对象")
    nested[key] = value


def test_success_maps_only_fourteen_fields_and_preserves_unicode(
    pull_payload: dict[str, object],
) -> None:
    pull_payload.update(
        body="RAW_SECRET",
        diff_url="http://127.0.0.1/private",
        patch_url="https://evil.invalid/patch",
        mergeable=None,
        merge_commit_sha="c3" * 20,
        url="https://evil.invalid/pull",
    )
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        result = client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert isinstance(result, GitHubPullRequestResponse)
        assert result.model_dump() == {
            "repository_url": REPOSITORY_URL,
            "pull_number": PULL_NUMBER,
            "title": "修复重试 🙂",
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": f"{REPOSITORY_URL}/pull/{PULL_NUMBER}",
            "base_ref": "发布/版本一",
            "head_ref": "feature/重试🚀",
            "base_sha": "a1" * 20,
            "head_sha": "b2" * 20,
            "changed_files": 2,
            "additions": 10,
            "deletions": 3,
        }
        assert len(upstream.requests) == 1
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("repository_url", "pull_number", "owner_repo"),
    [
        (REPOSITORY_URL, 1, "sample-owner/sample-repo"),
        ("HTTPS://GitHub.Com/PsF/ReQuEsTs/", 42, "PsF/ReQuEsTs"),
        ("https://github.com/Owner-1/Repo_1.2", 2_147_483_647, "Owner-1/Repo_1.2"),
    ],
)
def test_fixed_single_request_headers_and_existing_timeouts(
    pull_payload: dict[str, object], repository_url: str, pull_number: int, owner_repo: str
) -> None:
    html_url = f"HTTPS://GITHUB.COM/{owner_repo.upper()}/PULL/{pull_number}"
    pull_payload.update(number=pull_number, html_url=html_url)
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        result = client.fetch_pull_request(repository_url, pull_number)
        assert result.repository_url == repository_url
        assert result.html_url == html_url
        assert result.pull_number == pull_number
        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request.method == "GET"
        assert str(request.url) == f"https://api.github.com/repos/{owner_repo}/pulls/{pull_number}"
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


def test_existing_tls_redirect_and_environment_settings_are_preserved(
    monkeypatch: pytest.MonkeyPatch, pull_payload: dict[str, object]
) -> None:
    settings: dict[str, object] = {}
    original_client = httpx.Client

    def capture_settings(**kwargs: object) -> httpx.Client:
        settings.update(kwargs)
        return original_client(**kwargs)

    monkeypatch.setattr("app.github_client.httpx.Client", capture_settings)
    with mock_pull_response(json_body(pull_payload)) as (client, _upstream):
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).pull_number == PULL_NUMBER
    assert settings["base_url"] == "https://api.github.com"
    assert settings["verify"] is True
    assert settings["follow_redirects"] is False
    assert settings["trust_env"] is False


@pytest.mark.parametrize(
    "repository_url",
    [
        None,
        123,
        True,
        b"https://github.com/a/b",
        "",
        "http://github.com/a/b",
        "https://github.com.evil.invalid/a/b",
        "https://github.com@evil.invalid/a/b",
        "https://user:RAW_SECRET@github.com/a/b",
        "https://github.com:443/a/b",
        "https://github.com/a/b?RAW_SECRET",
        "https://github.com/a/b#fragment",
        "https://github.com/a/b/pull/42",
        "https://github.com/a/b.git",
        "https://github.com/a%2fb/c",
        "https://github.com/a\\b/c",
        " https://github.com/a/b",
        "https://github.com/a/b\n",
        "https://github.com/a/b\x00",
        "https://github.com//b",
        "https://github.com/a/..",
        "https://github.com/a/" + "b" * 2048,
    ],
)
def test_invalid_repository_never_reaches_transport(repository_url: str) -> None:
    with mock_pull_response() as (client, upstream):
        with pytest.raises(ValueError) as error:
            client.fetch_pull_request(repository_url, PULL_NUMBER)
        assert_safe_error(error.value, "仓库地址或 PR 编号无效。")
        assert error.value.__suppress_context__
        assert upstream.requests == []
        assert upstream.responses == []
        assert upstream.stream.consumed == []


@pytest.mark.parametrize(
    "pull_number", [0, -1, 2_147_483_648, True, False, "42", 42.0, None, b"42"]
)
def test_invalid_number_never_reaches_transport(pull_number: int) -> None:
    with mock_pull_response() as (client, upstream):
        with pytest.raises(ValueError) as error:
            client.fetch_pull_request(REPOSITORY_URL, pull_number)
        assert_safe_error(error.value, "仓库地址或 PR 编号无效。")
        assert error.value.__suppress_context__
        assert upstream.requests == []
        assert upstream.stream.consumed == []


@pytest.mark.parametrize("number", [1, 43, 0, True, False, "42", 42.0, None])
def test_number_must_be_a_matching_real_integer(
    pull_payload: dict[str, object], number: object
) -> None:
    pull_payload["number"] = number
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "html_url",
    [
        "http://github.com/sample-owner/sample-repo/pull/42",
        "https://evil.invalid/sample-owner/sample-repo/pull/42",
        "https://github.com.evil.invalid/sample-owner/sample-repo/pull/42",
        "https://github.com@evil.invalid/sample-owner/sample-repo/pull/42",
        "https://RAW_SECRET@github.com/sample-owner/sample-repo/pull/42",
        "https://github.com:443/sample-owner/sample-repo/pull/42",
        f"{REPOSITORY_URL}/pull/42?",
        f"{REPOSITORY_URL}/pull/42#RAW_SECRET",
        f"{REPOSITORY_URL}/pull/42/",
        f"{REPOSITORY_URL}/pull/42/files",
        f"{REPOSITORY_URL}/pull/43",
        f"{REPOSITORY_URL}/pull/042",
        f"{REPOSITORY_URL}/pull/%34%32",
        f"{REPOSITORY_URL}/pull\\42",
        "https://github.com/other-owner/sample-repo/pull/42",
        "https://github.com/sample-owner/other-repo/pull/42",
        f" {REPOSITORY_URL}/pull/42",
        f"{REPOSITORY_URL}/pull/42\n",
        "/sample-owner/sample-repo/pull/42",
    ],
)
def test_html_url_identity_is_checked_without_requesting_it(
    pull_payload: dict[str, object], html_url: str
) -> None:
    pull_payload["html_url"] = html_url
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert len(upstream.requests) == 1
        assert_response_closed(client, upstream)


def test_html_url_rejects_non_ascii_even_when_lowercase_matches(
    pull_payload: dict[str, object],
) -> None:
    repository_url = "https://github.com/sample-owner/kit"
    pull_payload["html_url"] = "https://github.com/sample-owner/\N{KELVIN SIGN}it/pull/42"

    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(repository_url, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert len(upstream.requests) == 1
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("field_name", UPSTREAM_FIELDS)
def test_every_required_upstream_field_must_exist(
    pull_payload: dict[str, object], field_name: str
) -> None:
    del pull_payload[field_name]
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("side", ["base", "head"])
@pytest.mark.parametrize("value", [None, [], "RAW_SECRET", 1, True])
def test_base_and_head_must_be_objects(
    pull_payload: dict[str, object], side: str, value: object
) -> None:
    pull_payload[side] = value
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("side", ["base", "head"])
@pytest.mark.parametrize("missing", ["ref", "sha"])
def test_nested_refs_and_shas_must_exist(
    pull_payload: dict[str, object], side: str, missing: str
) -> None:
    pull_payload[side] = {"sha": "a1" * 20} if missing == "ref" else {"ref": "main"}
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("title", ""),
        ("title", 1),
        ("title", None),
        ("state", "merged"),
        ("state", "OPEN"),
        ("state", None),
        ("draft", 0),
        ("draft", "false"),
        ("draft", None),
        ("merged", 1.0),
        ("merged", "true"),
        ("merged", None),
        ("html_url", ""),
        ("html_url", 123),
        ("html_url", None),
        ("base.ref", ""),
        ("base.ref", 123),
        ("base.ref", None),
        ("head.ref", ""),
        ("head.ref", False),
        ("head.ref", None),
        ("base.sha", None),
        ("head.sha", 123),
    ],
)
def test_strict_mapping_does_not_repair_invalid_values(
    pull_payload: dict[str, object], field_name: str, value: object
) -> None:
    set_upstream_field(pull_payload, field_name, value)
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("field_name", ["changed_files", "additions", "deletions"])
@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", None])
def test_counts_must_be_strict_nonnegative_integers(
    pull_payload: dict[str, object], field_name: str, value: object
) -> None:
    pull_payload[field_name] = value
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("field_name", ["base.sha", "head.sha"])
@pytest.mark.parametrize("sha", ["a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 39 + "\n"])
def test_only_complete_lowercase_shas_are_accepted(
    pull_payload: dict[str, object], field_name: str, sha: str
) -> None:
    set_upstream_field(pull_payload, field_name, sha)
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("include_repo", [False, True])
def test_head_repo_is_not_required_or_fetched(
    pull_payload: dict[str, object], include_repo: bool
) -> None:
    head: dict[str, object] = {"ref": "fork/中文", "sha": "b2" * 20}
    if include_repo:
        head["repo"] = None
    pull_payload["head"] = head
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        result = client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert result.head_ref == "fork/中文"
        assert result.head_sha == "b2" * 20
        assert len(upstream.requests) == 1
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("state", "draft", "merged"),
    [
        ("open", True, False),
        ("closed", False, False),
        ("closed", False, True),
        ("open", True, True),
    ],
)
def test_states_and_zero_counts_are_preserved_without_inference(
    pull_payload: dict[str, object], state: str, draft: bool, merged: bool
) -> None:
    pull_payload.update(
        state=state, draft=draft, merged=merged, changed_files=0, additions=0, deletions=0
    )
    with mock_pull_response(json_body(pull_payload)) as (client, upstream):
        result = client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert (result.state, result.draft, result.merged) == (state, draft, merged)
        assert (result.changed_files, result.additions, result.deletions) == (0, 0, 0)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("size", [BODY_LIMIT - 1, BODY_LIMIT])
@pytest.mark.parametrize("content_length", [None, "1", "9999999", "invalid", "-1"])
def test_body_limit_counts_real_bytes_without_trusting_content_length(
    pull_payload: dict[str, object], size: int, content_length: str | None
) -> None:
    body = json_body(pull_payload)
    body += b" " * (size - len(body))
    assert len(body) == size
    stream = TrackingStream([body[:100], body[100:]])
    headers = {} if content_length is None else {"Content-Length": content_length}
    with mock_pull_response(stream=stream, headers=headers) as (client, upstream):
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).pull_number == PULL_NUMBER
        assert stream.consumed == stream.steps
        if content_length is None:
            assert "Content-Length" not in upstream.responses[0].headers
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("single_chunk", [False, True])
def test_overflow_stops_reading_and_closes_response(status: int, single_chunk: bool) -> None:
    prefix = [b"x" * (BODY_LIMIT + 1)] if single_chunk else [b" " * BODY_LIMIT, b"x"]
    stream = TrackingStream([*prefix, httpx.ReadError("RAW_SECRET must not be consumed")])
    with mock_pull_response(status=status, stream=stream, headers={"Content-Length": "1"}) as (
        client,
        upstream,
    ):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert stream.consumed == prefix
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("split_offset", [1, 2])
def test_utf8_characters_can_cross_raw_chunk_boundaries(
    pull_payload: dict[str, object], split_offset: int
) -> None:
    body = json_body(pull_payload)
    split_at = body.index("修".encode("utf-8")) + split_offset
    stream = TrackingStream([body[:split_at], body[split_at:]])
    with mock_pull_response(stream=stream) as (client, upstream):
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).title == "修复重试 🙂"
        assert stream.consumed == stream.steps
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"RAW_SECRET not JSON",
        b'{"message": "RAW_SECRET"',
        b"\xff",
        '{"message":"RAW_SECRET"}'.encode("utf-16"),
        json_body(None),
        json_body([]),
        json_body("RAW_SECRET"),
        json_body(1),
        json_body(True),
        b"[" * 2000 + b"]" * 2000,
    ],
)
def test_invalid_encoding_json_or_non_object_is_a_safe_upstream_error(
    status: int, body: bytes
) -> None:
    with mock_pull_response(body, status=status) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
def test_recursion_error_is_suppressed_without_exposing_parser_details(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail_json(_body: str) -> NoReturn:
        raise RecursionError("RAW_SECRET parser details")

    with mock_pull_response(b"{}", status=status) as (client, upstream):
        with monkeypatch.context() as patch:
            patch.setattr("app.github_client.json.loads", fail_json)
            with pytest.raises(GitHubUpstreamError) as error:
                client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert error.value.__suppress_context__
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("field_name", ["title", "html_url", "base.ref", "head.ref"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"])
def test_returned_strings_must_be_strictly_utf8_encodable(
    pull_payload: dict[str, object], field_name: str, surrogate: str
) -> None:
    set_upstream_field(pull_payload, field_name, "RAW_SECRET" + surrogate)
    # 上游字节本身合法，孤立代理码点经 JSON 转义进入解析结果。
    body = json.dumps(pull_payload, ensure_ascii=True).encode("utf-8")
    with mock_pull_response(body) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    ("status", "error_type", "message"),
    [
        (404, GitHubNotFoundError, "仓库或 PR 不存在或不可公开访问。"),
        (429, GitHubRateLimitError, "GitHub 请求受到限流，请稍后重试。"),
        (201, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (204, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (400, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (401, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (422, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (500, GitHubUpstreamError, UPSTREAM_MESSAGE),
        (503, GitHubUpstreamError, UPSTREAM_MESSAGE),
    ],
)
def test_status_errors_do_not_inspect_encoding_or_consume_body(
    status: int, error_type: type[GitHubClientError], message: str
) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be read")])
    with mock_pull_response(status=status, stream=stream, headers={"Content-Encoding": "gzip"}) as (
        client,
        upstream,
    ):
        with pytest.raises(error_type) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, message)
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308, 399])
def test_redirects_are_neither_read_nor_followed(status: int) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be read")])
    with mock_pull_response(
        status=status,
        stream=stream,
        headers={"Location": "http://127.0.0.1/RAW_SECRET", "Content-Encoding": "gzip"},
    ) as (client, upstream):
        with pytest.raises(GitHubRedirectError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, "GitHub 返回了重定向，请使用仓库当前的主页地址。")
        assert len(upstream.requests) == 1
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("remaining", ["0", " 0 "])
def test_403_zero_remaining_takes_priority_without_body_reads(remaining: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be read")])
    with mock_pull_response(
        status=403,
        stream=stream,
        headers={"X-RateLimit-Remaining": remaining, "Content-Encoding": "gzip"},
    ) as (client, upstream):
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, "GitHub 请求受到限流，请稍后重试。")
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "message", ["API rate limit exceeded RAW_SECRET", "You exceeded a SECONDARY RATE LIMIT."]
)
def test_403_explicit_json_message_is_rate_limited(message: str) -> None:
    with mock_pull_response(
        json_body({"message": message}), status=403, headers={"X-RateLimit-Remaining": "8"}
    ) as (client, upstream):
        with pytest.raises(GitHubRateLimitError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, "GitHub 请求受到限流，请稍后重试。")
        assert upstream.stream.consumed == upstream.stream.steps
        assert_response_closed(client, upstream)


@pytest.mark.parametrize(
    "payload", [{}, {"message": None}, {"message": 1}, {"message": "RAW_SECRET"}]
)
@pytest.mark.parametrize("remaining", ["", "8", "00"])
def test_ordinary_403_is_forbidden_without_inventing_rate_limit_evidence(
    payload: object, remaining: str
) -> None:
    with mock_pull_response(
        json_body(payload),
        status=403,
        headers={"X-RateLimit-Remaining": remaining, "Retry-After": "60"},
    ) as (client, upstream):
        with pytest.raises(GitHubForbiddenError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, "GitHub 拒绝访问该仓库或 PR。")
        assert upstream.stream.consumed == upstream.stream.steps
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("status", [200, 403])
@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip", ""])
def test_non_identity_encoding_is_rejected_before_body_reads(status: int, encoding: str) -> None:
    stream = TrackingStream([httpx.ReadError("RAW_SECRET must not be read")])
    with mock_pull_response(
        status=status, stream=stream, headers={"Content-Encoding": encoding}
    ) as (client, upstream):
        with pytest.raises(GitHubUpstreamError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, UPSTREAM_MESSAGE)
        assert stream.consumed == []
        assert_response_closed(client, upstream)


@pytest.mark.parametrize("encoding", ["identity", " Identity "])
def test_explicit_identity_is_accepted(pull_payload: dict[str, object], encoding: str) -> None:
    with mock_pull_response(json_body(pull_payload), headers={"Content-Encoding": encoding}) as (
        client,
        upstream,
    ):
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).pull_number == PULL_NUMBER
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
        (httpx.RequestError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
def test_connection_errors_are_sanitized_without_retry_or_closing_client(
    httpx_error: type[httpx.RequestError], client_error: type[GitHubClientError], message: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx_error("RAW_SECRET connection details", request=request)

    transport = TrackingTransport(handler)
    with GitHubRepositoryClient(transport=transport) as client:
        with pytest.raises(client_error) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, message)
        assert error.value.__suppress_context__
        assert len(requests) == 1
        assert not client.is_closed
        assert transport.close_count == 0
    assert client.is_closed
    assert transport.close_count == 1


@pytest.mark.parametrize(
    ("httpx_error", "client_error", "message"),
    [
        (httpx.ReadTimeout, GitHubTimeoutError, "GitHub 请求超时。"),
        (httpx.ReadError, GitHubNetworkError, "GitHub 网络请求失败。"),
        (httpx.RemoteProtocolError, GitHubNetworkError, "GitHub 网络请求失败。"),
    ],
)
@pytest.mark.parametrize("phase", ["read", "exit", "body-complete"])
def test_read_and_close_errors_are_sanitized_and_release_response(
    pull_payload: dict[str, object],
    httpx_error: type[httpx.RequestError],
    client_error: type[GitHubClientError],
    message: str,
    phase: str,
) -> None:
    network_error = httpx_error("RAW_SECRET stream details")
    if phase == "read":
        stream = TrackingStream([b'{"number":', network_error, b"must not be consumed"])
        status = 200
        consumed = stream.steps[:2]
    elif phase == "exit":
        stream = TrackingStream([b"must not be consumed"], close_error=network_error)
        status = 404
        consumed = []
    else:
        stream = TrackingStream([json_body(pull_payload)], close_error=network_error)
        status = 200
        consumed = stream.steps
    with mock_pull_response(status=status, stream=stream) as (client, upstream):
        with pytest.raises(client_error) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert_safe_error(error.value, message)
        assert error.value.__suppress_context__
        assert stream.consumed == consumed
        assert_response_closed(client, upstream)


def test_programming_errors_are_not_misclassified_as_network_errors() -> None:
    programming_error = RuntimeError("test programming error")

    def handler(_request: httpx.Request) -> httpx.Response:
        raise programming_error

    with GitHubRepositoryClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as error:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert error.value is programming_error
        assert not client.is_closed


def test_client_reuse_and_request_header_isolation(pull_payload: dict[str, object]) -> None:
    requests: list[httpx.Request] = []
    streams: list[TrackingStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status = 200
        if len(requests) == 1:
            status = 403
            payload: object = {"message": "Forbidden"}
        elif request.url.path.endswith(f"/pulls/{PULL_NUMBER}"):
            payload = pull_payload
        elif request.url.path.endswith("/contents/src/api.py"):
            payload = {
                "type": "file",
                "encoding": "base64",
                "path": "src/api.py",
                "name": "api.py",
                "size": 6,
                "content": base64.b64encode(b"x = 1\n").decode("ascii"),
            }
        elif request.url.path == "/repos/sample-owner/sample-repo":
            payload = {
                "full_name": "sample-owner/sample-repo",
                "description": None,
                "language": "Python",
                "stargazers_count": 1,
                "default_branch": "main",
                "archived": False,
                "html_url": REPOSITORY_URL,
            }
        else:
            pytest.fail(f"意外的请求路径：{request.url.path}")
        stream = TrackingStream([json_body(payload)])
        streams.append(stream)
        return httpx.Response(status, stream=stream)

    transport = TrackingTransport(handler)
    client = GitHubRepositoryClient(transport=transport)
    try:
        with pytest.raises(GitHubForbiddenError):
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).pull_number == PULL_NUMBER
        assert client.fetch_repository(REPOSITORY_URL).stars == 1
        assert client.fetch_python_file(REPOSITORY_URL, "src/api.py") == "x = 1\n"
        assert client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER).pull_number == PULL_NUMBER
        assert len(requests) == 5
        assert [request.headers["Accept-Encoding"] for request in requests[:2]] == ["identity"] * 2
        assert requests[2].headers["Accept-Encoding"] != "identity"
        assert requests[3].headers["Accept-Encoding"] == "identity"
        assert requests[4].headers["Accept-Encoding"] == "identity"
        assert all(stream.close_count == 1 for stream in streams)
        assert not client.is_closed
        assert transport.close_count == 0
    finally:
        client.close()
    assert client.is_closed
    assert transport.close_count == 1
    client.close()
    assert transport.close_count == 1
    with pytest.raises(RuntimeError):
        client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
    assert len(requests) == 5


def test_context_exit_closes_client_after_pr_error() -> None:
    stream = TrackingStream([b"must not be consumed"])
    upstream = MockUpstream(stream, 404, {})
    transport = TrackingTransport(upstream.respond)
    client = GitHubRepositoryClient(transport=transport)
    with pytest.raises(GitHubNotFoundError):
        with client:
            client.fetch_pull_request(REPOSITORY_URL, PULL_NUMBER)
    assert client.is_closed
    assert transport.close_count == 1
    assert stream.close_count == 1
    assert stream.consumed == []
