"""同步查询 GitHub 公开仓库元数据和受限文件，不负责路由或源码执行。"""

import base64
import binascii
import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import httpx
from pydantic import ValidationError

from .schemas import (
    GitHubPullRequestFilesResponse,
    GitHubPullRequestResponse,
    GitHubRepositoryResponse,
    parse_github_repository_url,
    validate_github_file_path,
    validate_github_pull_number,
    validate_github_pull_request_file_paths,
    validate_github_pull_request_files_page,
)

_MAX_FILE_RESPONSE_BYTES = 65_536
_MAX_PYTHON_FILE_BYTES = 20_000
_MAX_BASE64_CHARACTERS = 26_668
_FILE_UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的文件响应。"
_MAX_PR_RESPONSE_BYTES = 1_048_576
_PR_UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 响应。"
_MAX_PR_FILES_RESPONSE_BYTES = 1_048_576
_PR_FILES_UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件列表响应。"
_PR_FILE_UPSTREAM_MESSAGE = "GitHub 返回了无效或非预期的 PR 文件读取响应。"


class GitHubClientError(Exception):
    """GitHub 客户端业务异常的共同基类，供路由统一捕获。"""


class GitHubNotFoundError(GitHubClientError):
    """仓库不存在或无法匿名公开读取。"""


class GitHubRateLimitError(GitHubClientError):
    """GitHub 明确报告请求受到限流。"""


class GitHubForbiddenError(GitHubClientError):
    """GitHub 拒绝访问，且没有足够证据认定为限流。"""


class GitHubRedirectError(GitHubClientError):
    """GitHub 返回重定向；客户端不会请求 Location 中的地址。"""


class GitHubUpstreamError(GitHubClientError):
    """上游状态或仓库数据不符合约定。"""


class GitHubTimeoutError(GitHubClientError):
    """连接、读取、写入或等待连接池超时。"""


class GitHubNetworkError(GitHubClientError):
    """发生超时以外的 HTTPX 请求错误。"""


class GitHubFileTooLargeError(GitHubClientError):
    """源码或 Base64 内容超过约定的文件大小限制。"""


class GitHubUnsupportedFileError(GitHubClientError):
    """文件类型或编码不在本客户端支持范围内。"""


class GitHubPullRequestSourceUnavailableError(GitHubClientError):
    """PR 来源仓库已不可用或不是公开仓库，不能回退读取其他版本。"""


@dataclass(frozen=True)
class GitHubPullRequestPythonFile:
    """供应用内部使用的读取结果；不包含审查结果，也不在 repr 中显示源码。"""

    head_repository_url: str
    head_sha: str
    code: str = field(repr=False)


@dataclass(frozen=True)
class GitHubPullRequestPythonFileContent:
    """批量结果中的一个文件；源码仅供后续静态分析，不显示在 repr 中。"""

    file_path: str
    code: str = field(repr=False)


@dataclass(frozen=True)
class GitHubPullRequestPythonFiles:
    """所有文件共用同一个来源和 SHA；元组及冻结条目保留读取顺序。"""

    head_repository_url: str
    head_sha: str
    files: tuple[GitHubPullRequestPythonFileContent, ...]


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    """JSON 只能作为待验证数据；解析失败时不暴露响应正文或异常文本。"""
    try:
        payload: object = response.json()
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_rate_limited(response: httpx.Response) -> bool:
    """只用于 403：额度为零或消息明确说明限流，才认定为限流。"""
    if response.headers.get("X-RateLimit-Remaining", "").strip() == "0":
        return True
    payload = _json_object(response)
    if payload is None:
        return False
    message = payload.get("message")
    if not isinstance(message, str):
        return False
    message = message.lower()
    return "api rate limit" in message or "secondary rate limit" in message


def _read_file_response(response: httpx.Response) -> bytes:
    """只收集受限的原始字节，不解压，也不依赖 Content-Length。"""
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)
    body = bytearray()
    for chunk in response.iter_raw():
        # 先检查再加入缓冲区；发现超限后不再消费后面的流块。
        if len(body) + len(chunk) > _MAX_FILE_RESPONSE_BYTES:
            raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)
        body.extend(chunk)
    return bytes(body)


def _file_json_object(body: bytes) -> dict[str, object] | None:
    """只解析自行收集的有界正文；严格 UTF-8，不猜测 JSON 编码。"""
    try:
        payload: object = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _decode_python_file(payload: dict[str, object], file_path: str) -> str:
    """验证 Contents 数据并解码，不解析 AST、不执行或保存源码。"""
    file_type = payload.get("type")
    encoding = payload.get("encoding")
    size = payload.get("size")
    path = payload.get("path")
    name = payload.get("name")
    content = payload.get("content")
    if (
        not isinstance(file_type, str)
        or not isinstance(encoding, str)
        or type(size) is not int
        or size < 0
        or not isinstance(path, str)
        or not isinstance(name, str)
        or not isinstance(content, str)
        or path != file_path
        or name != file_path.rsplit("/", 1)[-1]
    ):
        raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)
    # Contents API 可能返回仓库内符号链接目标的普通文件内容。
    # 这里只拒绝显式链接/子模块标记，不能证明原路径一定不是符号链接。
    if (
        file_type != "file"
        or encoding != "base64"
        or "target" in payload
        or payload.get("submodule_git_url") is not None
    ):
        raise GitHubUnsupportedFileError("GitHub 文件类型或编码不受支持。")
    if size > _MAX_PYTHON_FILE_BYTES:
        raise GitHubFileTooLargeError("Python 文件超过大小限制。")

    # 只移除协议允许的 CR/LF；空格、Tab 等不能被悄悄清理掉。
    content = content.replace("\r", "").replace("\n", "")
    if len(content) > _MAX_BASE64_CHARACTERS:
        raise GitHubFileTooLargeError("Python 文件超过大小限制。")
    try:
        source = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError):
        raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE) from None
    if len(source) > _MAX_PYTHON_FILE_BYTES:
        raise GitHubFileTooLargeError("Python 文件超过大小限制。")
    if len(source) != size:
        raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError:
        raise GitHubUnsupportedFileError("GitHub 文件类型或编码不受支持。") from None


def _read_pull_request_response(response: httpx.Response) -> dict[str, object]:
    """收集有界原始正文，严格按 UTF-8 和 JSON 对象解析，不访问响应中的链接。"""
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)
    body = bytearray()
    for chunk in response.iter_raw():
        # 1 MiB 是项目策略；不能相信 Content-Length，也不能先加入再判断超限。
        if len(body) + len(chunk) > _MAX_PR_RESPONSE_BYTES:
            raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)
        body.extend(chunk)
    try:
        payload: object = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE) from None
    if not isinstance(payload, dict):
        raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)
    return payload


def _pull_request_overview(
    payload: dict[str, object],
    repository_url: str,
    pull_number: int,
    expected_html_url: str,
) -> GitHubPullRequestResponse:
    """显式映射平铺字段，验证类型、可编码性与 PR 身份，不推导合并状态。"""
    try:
        base = payload["base"]
        head = payload["head"]
        if not isinstance(base, dict) or not isinstance(head, dict):
            raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)
        result = GitHubPullRequestResponse.model_validate(
            {
                "repository_url": repository_url,
                "pull_number": payload["number"],
                "title": payload["title"],
                "state": payload["state"],
                "draft": payload["draft"],
                "merged": payload["merged"],
                "html_url": payload["html_url"],
                "base_ref": base["ref"],
                "head_ref": head["ref"],
                "base_sha": base["sha"],
                "head_sha": head["sha"],
                "changed_files": payload["changed_files"],
                "additions": payload["additions"],
                "deletions": payload["deletions"],
            }
        )
        # JSON 转义可能包含孤立代理码点；不能把无法严格编码的字符串交给路由。
        for value in result.model_dump().values():
            if isinstance(value, str):
                value.encode("utf-8")
    except (KeyError, ValidationError, UnicodeEncodeError):
        raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE) from None
    if (
        result.pull_number != pull_number
        or not result.html_url.isascii()
        or result.html_url.lower() != expected_html_url.lower()
    ):
        raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)
    return result


def _read_pull_request_files_response(response: httpx.Response) -> object:
    """限制全部原始正文，包括不返回的 patch；不解压，不信任 Content-Length。"""
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)
    body = bytearray()
    for chunk in response.iter_raw():
        if len(body) + len(chunk) > _MAX_PR_FILES_RESPONSE_BYTES:
            raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)
        body.extend(chunk)
    try:
        payload: object = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE) from None
    return payload


def _pull_request_files_page(
    payload: object, repository_url: str, pull_number: int, page: int
) -> GitHubPullRequestFilesResponse:
    """只映射元数据；文件名不作为下载路径，不推断改名关系或下一页。"""
    if not isinstance(payload, list) or len(payload) > 30:
        raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)
    files: list[dict[str, object]] = []
    try:
        for item in payload:
            if not isinstance(item, dict):
                raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)
            files.append(
                {
                    "filename": item["filename"],
                    "status": item["status"],
                    "additions": item["additions"],
                    "deletions": item["deletions"],
                    "changes": item["changes"],
                    "previous_filename": item.get("previous_filename"),
                }
            )
        result = GitHubPullRequestFilesResponse.model_validate(
            {
                "repository_url": repository_url,
                "pull_number": pull_number,
                "page": page,
                "per_page": 30,
                "files": files,
            }
        )
        # JSON 转义可能包含孤立代理码点，必须在交给路由之前拒绝。
        for file in result.files:
            file.filename.encode("utf-8")
            if file.previous_filename is not None:
                file.previous_filename.encode("utf-8")
    except (KeyError, ValidationError, UnicodeEncodeError):
        raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE) from None
    return result


def _pull_request_file_source(
    payload: dict[str, object], repository_url: str, pull_number: int, expected_html_url: str
) -> tuple[str, str]:
    """沿用 PR 身份验证，只从经验证的来源仓库名构造地址，不访问上游链接。"""
    try:
        overview = _pull_request_overview(payload, repository_url, pull_number, expected_html_url)
        head = payload["head"]
        if not isinstance(head, dict):
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
        source = head["repo"]
        if source is None:
            raise GitHubPullRequestSourceUnavailableError("PR 来源仓库不可用于公开文件审查。")
        if not isinstance(source, dict):
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
        full_name = source["full_name"]
        private = source["private"]
        if not isinstance(full_name, str) or type(private) is not bool:
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
        owner, repo = parse_github_repository_url(f"https://github.com/{full_name}")
        # full_name 必须恰好是两个路径段，不能接受 URL 规则允许的末尾斜杠。
        if full_name != f"{owner}/{repo}":
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
        if private:
            raise GitHubPullRequestSourceUnavailableError("PR 来源仓库不可用于公开文件审查。")
    except (KeyError, ValueError, GitHubUpstreamError):
        raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE) from None
    return f"https://github.com/{owner}/{repo}", overview.head_sha


class GitHubRepositoryClient:
    """固定访问 api.github.com；transport 仅供受信任的应用代码和测试注入。"""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url="https://api.github.com",
            follow_redirects=False,
            verify=True,
            # 不隐式使用环境中的代理或证书配置，保持请求行为明确。
            trust_env=False,
            timeout=httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0),
            headers={
                "User-Agent": "RepoPilot",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            transport=transport,
        )

    def fetch_repository(self, repository_url: str) -> GitHubRepositoryResponse:
        """返回七字段模型；无效 URL 沿用解析函数的 ValueError，且不会发请求。"""
        owner, repo = parse_github_repository_url(repository_url)
        try:
            # 仅把已验证的路径段加入固定 base_url，不请求用户传入的完整 URL。
            response = self._client.get(f"/repos/{owner}/{repo}")
        except httpx.TimeoutException:
            raise GitHubTimeoutError("GitHub 请求超时。") from None
        except httpx.RequestError:
            raise GitHubNetworkError("GitHub 网络请求失败。") from None

        status = response.status_code
        if status == 404:
            raise GitHubNotFoundError("仓库不存在或不可公开访问。")
        if status == 429:
            raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
        if status == 403:
            if _is_rate_limited(response):
                raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
            raise GitHubForbiddenError("GitHub 拒绝访问该仓库。")
        if 300 <= status < 400:
            raise GitHubRedirectError("GitHub 返回了重定向，请使用仓库当前的主页地址。")
        if status != 200:
            raise GitHubUpstreamError("GitHub 返回了无效或非预期的响应。")

        payload = _json_object(response)
        if payload is None:
            raise GitHubUpstreamError("GitHub 返回了无效或非预期的响应。")
        try:
            # 使用 [] 保证字段缺失会报错；允许 null 不代表允许省略字段。
            # 只映射约定字段，不能把 GitHub 的整个 JSON 交给响应模型。
            return GitHubRepositoryResponse.model_validate(
                {
                    "full_name": payload["full_name"],
                    "description": payload["description"],
                    "language": payload["language"],
                    "stars": payload["stargazers_count"],
                    "default_branch": payload["default_branch"],
                    "archived": payload["archived"],
                    "html_url": payload["html_url"],
                }
            )
        except (KeyError, ValidationError):
            raise GitHubUpstreamError("GitHub 返回了无效或非预期的响应。") from None

    def fetch_python_file(self, repository_url: str, file_path: str) -> str:
        """单次读取默认分支文件；响应始终关闭，整个客户端仍可复用。"""
        try:
            if not isinstance(repository_url, str):
                raise ValueError("仓库地址必须是字符串。")
            owner, repo = parse_github_repository_url(repository_url)
            validate_github_file_path(file_path)
        except ValueError:
            raise ValueError("仓库地址或 Python 文件路径无效。") from None

        # try 覆盖建立连接、迭代正文和 with 退出，避免漏掉读取/关闭时的网络错误。
        try:
            with self._client.stream(
                "GET",
                f"/repos/{owner}/{repo}/contents/{file_path}",
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if status == 404:
                    raise GitHubNotFoundError("仓库或文件不存在或不可公开访问。")
                if status == 429:
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if (
                    status == 403
                    and response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
                ):
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if 300 <= status < 400:
                    raise GitHubRedirectError("GitHub 返回了重定向，请使用仓库当前的主页地址。")
                if status not in {200, 403}:
                    raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)

                payload = _file_json_object(_read_file_response(response))
                if status == 403:
                    message = payload.get("message") if payload is not None else None
                    if isinstance(message, str) and (
                        "api rate limit" in message.lower()
                        or "secondary rate limit" in message.lower()
                    ):
                        raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                    raise GitHubForbiddenError("GitHub 拒绝访问该仓库。")
                if payload is None:
                    raise GitHubUpstreamError(_FILE_UPSTREAM_MESSAGE)
                return _decode_python_file(payload, file_path)
        except httpx.TimeoutException:
            raise GitHubTimeoutError("GitHub 请求超时。") from None
        except httpx.RequestError:
            raise GitHubNetworkError("GitHub 网络请求失败。") from None

    def fetch_pull_request(
        self, repository_url: str, pull_number: int
    ) -> GitHubPullRequestResponse:
        """单次读取受限的 PR 概览；输入先验证，响应关闭后客户端仍可复用。"""
        try:
            if not isinstance(repository_url, str):
                raise ValueError("仓库地址必须是字符串。")
            owner, repo = parse_github_repository_url(repository_url)
            validate_github_pull_number(pull_number)
        except ValueError:
            raise ValueError("仓库地址或 PR 编号无效。") from None

        # try 同时覆盖连接、流式读取及 with 退出时的 HTTPX 错误。
        try:
            with self._client.stream(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pull_number}",
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if status == 404:
                    raise GitHubNotFoundError("仓库或 PR 不存在或不可公开访问。")
                if status == 429:
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if (
                    status == 403
                    and response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
                ):
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if 300 <= status < 400:
                    raise GitHubRedirectError("GitHub 返回了重定向，请使用仓库当前的主页地址。")
                if status not in {200, 403}:
                    raise GitHubUpstreamError(_PR_UPSTREAM_MESSAGE)

                payload = _read_pull_request_response(response)
                if status == 403:
                    message = payload.get("message")
                    if isinstance(message, str) and (
                        "api rate limit" in message.lower()
                        or "secondary rate limit" in message.lower()
                    ):
                        raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                    raise GitHubForbiddenError("GitHub 拒绝访问该仓库或 PR。")
                return _pull_request_overview(
                    payload,
                    repository_url,
                    pull_number,
                    f"https://github.com/{owner}/{repo}/pull/{pull_number}",
                )
        except httpx.TimeoutException:
            raise GitHubTimeoutError("GitHub 请求超时。") from None
        except httpx.RequestError:
            raise GitHubNetworkError("GitHub 网络请求失败。") from None

    def fetch_pull_request_files(
        self, repository_url: str, pull_number: int, page: int = 1
    ) -> GitHubPullRequestFilesResponse:
        """单次读取一页 PR 文件元数据；不查询 PR 概览，不自动翻页或下载文件。"""
        try:
            if not isinstance(repository_url, str):
                raise ValueError("仓库地址必须是字符串。")
            owner, repo = parse_github_repository_url(repository_url)
            validate_github_pull_number(pull_number)
            validate_github_pull_request_files_page(page)
        except ValueError:
            raise ValueError("仓库地址、PR 编号或页码无效。") from None

        # 同时覆盖请求建立、逐块读取以及 with 退出阶段的 HTTPX 异常。
        try:
            with self._client.stream(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": 30, "page": page},
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if status == 404:
                    raise GitHubNotFoundError("仓库或 PR 不存在或不可公开访问。")
                if status == 429:
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if (
                    status == 403
                    and response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
                ):
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if 300 <= status < 400:
                    raise GitHubRedirectError("GitHub 返回了重定向，请使用仓库当前的主页地址。")
                if status not in {200, 403}:
                    raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)

                payload = _read_pull_request_files_response(response)
                if status == 403:
                    if not isinstance(payload, dict):
                        raise GitHubUpstreamError(_PR_FILES_UPSTREAM_MESSAGE)
                    message = payload.get("message")
                    if isinstance(message, str) and (
                        "api rate limit" in message.lower()
                        or "secondary rate limit" in message.lower()
                    ):
                        raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                    raise GitHubForbiddenError("GitHub 拒绝访问该仓库或 PR。")
                return _pull_request_files_page(payload, repository_url, pull_number, page)
        except httpx.TimeoutException:
            raise GitHubTimeoutError("GitHub 请求超时。") from None
        except httpx.RequestError:
            raise GitHubNetworkError("GitHub 网络请求失败。") from None

    def fetch_pull_request_python_file(
        self, repository_url: str, pull_number: int, file_path: str
    ) -> GitHubPullRequestPythonFile:
        """先确定 PR 来源和 SHA，再读取该提交的一个文件；不判断它是否属于变更列表。"""
        try:
            if not isinstance(repository_url, str):
                raise ValueError("仓库地址必须是字符串。")
            owner, repo = parse_github_repository_url(repository_url)
            validate_github_pull_number(pull_number)
            validate_github_file_path(file_path)
        except ValueError:
            raise ValueError("仓库地址、PR 编号或 Python 文件路径无效。") from None

        payload = self._fetch_pull_request_review_object(
            f"/repos/{owner}/{repo}/pulls/{pull_number}"
        )
        source_url, head_sha = _pull_request_file_source(
            payload,
            repository_url,
            pull_number,
            f"https://github.com/{owner}/{repo}/pull/{pull_number}",
        )
        source_owner, source_repo = parse_github_repository_url(source_url)
        # 第一份响应已关闭；ref 只使用已验证的完整 SHA，不使用会移动的分支名。
        file_payload = self._fetch_pull_request_review_object(
            f"/repos/{source_owner}/{source_repo}/contents/{file_path}", file_ref=head_sha
        )
        try:
            code = _decode_python_file(file_payload, file_path)
        except GitHubUpstreamError:
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE) from None
        return GitHubPullRequestPythonFile(source_url, head_sha, code)

    def fetch_pull_request_python_files(
        self, repository_url: str, pull_number: int, file_paths: list[str]
    ) -> GitHubPullRequestPythonFiles:
        """只查询一次 PR，再按同一个 SHA 顺序读取指定文件；失败时不返回部分结果。"""
        try:
            if not isinstance(repository_url, str):
                raise ValueError("仓库地址必须是字符串。")
            owner, repo = parse_github_repository_url(repository_url)
            validate_github_pull_number(pull_number)
            # 验证全部路径并复制列表，后续读取不受调用方修改原列表的影响。
            paths = validate_github_pull_request_file_paths(file_paths)
        except ValueError:
            raise ValueError("仓库地址、PR 编号或 Python 文件路径列表无效。") from None

        payload = self._fetch_pull_request_review_object(
            f"/repos/{owner}/{repo}/pulls/{pull_number}"
        )
        source_url, head_sha = _pull_request_file_source(
            payload,
            repository_url,
            pull_number,
            f"https://github.com/{owner}/{repo}/pull/{pull_number}",
        )
        source_owner, source_repo = parse_github_repository_url(source_url)
        files: list[GitHubPullRequestPythonFileContent] = []
        for file_path in paths:
            # 不循环调用单文件方法，避免每个文件重新查询并取得不同的 PR 头提交。
            file_payload = self._fetch_pull_request_review_object(
                f"/repos/{source_owner}/{source_repo}/contents/{file_path}", file_ref=head_sha
            )
            try:
                code = _decode_python_file(file_payload, file_path)
            except GitHubUpstreamError:
                raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE) from None
            files.append(GitHubPullRequestPythonFileContent(file_path, code))
        return GitHubPullRequestPythonFiles(source_url, head_sha, tuple(files))

    def _fetch_pull_request_review_object(
        self, path: str, *, file_ref: str | None = None
    ) -> dict[str, object]:
        """仅供两阶段读取使用；两种正文分别受现有 PR 和文件限制约束。"""
        try:
            with self._client.stream(
                "GET",
                path,
                params={"ref": file_ref} if file_ref is not None else None,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                status = response.status_code
                if status == 404:
                    raise GitHubNotFoundError("仓库、PR、提交或文件不存在或不可公开访问。")
                if status == 429 or (
                    status == 403
                    and response.headers.get("X-RateLimit-Remaining", "").strip() == "0"
                ):
                    raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                if 300 <= status < 400:
                    raise GitHubRedirectError("GitHub 返回了重定向，请使用仓库当前的主页地址。")
                if status not in {200, 403}:
                    raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
                payload: dict[str, object] | None
                if file_ref is None:
                    payload = _read_pull_request_response(response)
                else:
                    payload = _file_json_object(_read_file_response(response))
                    if payload is None:
                        raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE)
                if status == 403:
                    message = payload.get("message")
                    if isinstance(message, str) and (
                        "api rate limit" in message.lower()
                        or "secondary rate limit" in message.lower()
                    ):
                        raise GitHubRateLimitError("GitHub 请求受到限流，请稍后重试。")
                    raise GitHubForbiddenError("GitHub 拒绝访问该仓库、PR 或文件。")
                return payload
        except httpx.TimeoutException:
            raise GitHubTimeoutError("GitHub 请求超时。") from None
        except httpx.RequestError:
            raise GitHubNetworkError("GitHub 网络请求失败。") from None
        except GitHubUpstreamError:
            raise GitHubUpstreamError(_PR_FILE_UPSTREAM_MESSAGE) from None

    @property
    def is_closed(self) -> bool:
        """公开连接关闭状态，调用方和测试无需读取私有属性。"""
        return self._client.is_closed

    def close(self) -> None:
        """关闭连接及传输层，可重复调用。"""
        self._client.close()

    def __enter__(self) -> Self:
        self._client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._client.__exit__(exc_type, exc_value, traceback)
