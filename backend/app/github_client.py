"""同步查询 GitHub 公开仓库元数据，不负责 FastAPI 路由或源码执行。"""

from types import TracebackType
from typing import Self

import httpx
from pydantic import ValidationError

from .schemas import GitHubRepositoryResponse, parse_github_repository_url


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
