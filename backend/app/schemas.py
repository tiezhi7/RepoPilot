"""定义 API 接收和返回的数据格式。"""

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_GITHUB_REPOSITORY_URL_LENGTH = 2048
_MAX_GITHUB_FILE_PATH_LENGTH = 512


class ReviewRequest(BaseModel):
    """只接收 Python 源码，不把其他类型自动转换成字符串。"""

    code: str = Field(strict=True, min_length=1, max_length=20_000)


class ReviewIssue(BaseModel):
    """一条审查发现，行号从 1 开始。"""

    rule_id: Literal["PY_EVAL", "PY_PRINT", "PY_BARE_EXCEPT", "PY_SYNTAX"]
    severity: Literal["error", "warning", "info"]
    line: int = Field(ge=1)
    message: str


class ReviewResponse(BaseModel):
    """Python 源码的审查结果。"""

    language: Literal["python"]
    issue_count: int = Field(ge=0)
    issues: list[ReviewIssue]


def parse_github_repository_url(repository_url: str) -> tuple[str, str]:
    """校验 GitHub 仓库主页地址，只返回 owner、repo，不查询网络。"""
    if not 1 <= len(repository_url) <= _MAX_GITHUB_REPOSITORY_URL_LENGTH:
        raise ValueError("仓库地址长度必须为 1–2048 个字符。")

    # urlsplit 会移除部分空白和控制字符，因此必须先检查原始字符串。
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in repository_url):
        raise ValueError("仓库地址不能包含空白或控制字符。")
    if "%" in repository_url or "\\" in repository_url:
        raise ValueError("仓库地址不能包含百分号编码或反斜杠。")

    try:
        parsed = urlsplit(repository_url)
    except ValueError as error:
        raise ValueError("仓库地址格式无效。") from error

    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("仓库地址必须使用 HTTPS，且域名必须为 github.com。")
    # 精确检查整个域名部分，同时拒绝 userinfo、显式端口及空端口冒号。
    if parsed.netloc.lower() != "github.com":
        raise ValueError("仓库地址不能包含用户信息或端口。")
    # 同时拒绝空的 ? 和 #，它们解析后的 query、fragment 都可能为空。
    if parsed.query or parsed.fragment or "?" in repository_url or "#" in repository_url:
        raise ValueError("仓库地址不能包含查询参数或片段。")

    # 只移除一个末尾斜杠，避免把多个末尾斜杠误当成合法路径。
    parts = parsed.path.removesuffix("/").split("/")
    if len(parts) != 3 or parts[0] != "" or not parts[1] or not parts[2]:
        raise ValueError("仓库路径必须为 /owner/repo，可带一个末尾斜杠。")
    owner, repo = parts[1], parts[2]

    # owner 仅允许 ASCII 字母、数字，以及不连续且不在两端的连字符。
    if re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", owner) is None:
        raise ValueError("owner 只能包含字母、数字和单个内部连字符。")
    if repo in {".", ".."} or re.fullmatch(r"[A-Za-z0-9_.-]+", repo) is None:
        raise ValueError("repo 只能包含字母、数字、点、下划线和连字符，且不能为 . 或 ..。")
    if repo.lower().endswith(".git"):
        raise ValueError("请提供仓库主页地址，而不是以 .git 结尾的克隆地址。")

    # 后续客户端应使用这两个已验证的路径段构造固定 API 地址。
    return owner, repo


class GitHubRepositoryRequest(BaseModel):
    """只接收经过专项校验的 GitHub 仓库 URL，不接收 Token 或 API 地址。"""

    model_config = ConfigDict(extra="forbid")

    repository_url: str = Field(
        strict=True, min_length=1, max_length=_MAX_GITHUB_REPOSITORY_URL_LENGTH
    )

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        """类型和长度校验通过后，再检查 URL；保留原始合法字符串。"""
        parse_github_repository_url(value)
        return value


class GitHubRepositoryResponse(BaseModel):
    """仅包含约定的七个字段；上游原始数据的映射由后续客户端负责。"""

    # 仅影响新模型，避免把字符串、布尔值或浮点数悄悄转换成目标类型。
    model_config = ConfigDict(strict=True, extra="forbid")

    full_name: str
    description: str | None
    language: str | None
    stars: int = Field(ge=0)
    default_branch: str
    archived: bool
    html_url: str


def validate_github_file_path(file_path: str) -> str:
    """校验仓库内 Python 文件的相对路径，不访问文件系统，也不规范化输入。"""
    if not isinstance(file_path, str) or not 1 <= len(file_path) <= _MAX_GITHUB_FILE_PATH_LENGTH:
        raise ValueError("文件路径必须是长度为 1–512 的字符串。")

    # 先检查原始字符；白名单同时排除空白、控制字符、编码符号及非 ASCII 字符。
    if re.fullmatch(r"[A-Za-z0-9_./-]+", file_path) is None:
        raise ValueError("文件路径只能包含 ASCII 字母、数字、点、下划线、连字符和 /。")

    # 不清理空段或点段：拒绝绝对路径、重复斜杠和路径穿越，但允许 package..py。
    if any(part in {"", ".", ".."} for part in file_path.split("/")):
        raise ValueError("文件路径必须是相对路径，且不能包含空路径段、. 或 ..。")
    if not file_path.endswith(".py"):
        raise ValueError("文件路径必须以小写 .py 结尾。")

    return file_path


class GitHubFileReviewRequest(BaseModel):
    """仅接收仓库主页地址和仓库内 Python 文件路径。"""

    model_config = ConfigDict(extra="forbid")

    repository_url: str = Field(
        strict=True, min_length=1, max_length=_MAX_GITHUB_REPOSITORY_URL_LENGTH
    )
    file_path: str = Field(strict=True, min_length=1, max_length=_MAX_GITHUB_FILE_PATH_LENGTH)

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        """复用已有 URL 规则，保留合法 URL 的原始形式。"""
        parse_github_repository_url(value)
        return value

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return validate_github_file_path(value)


class GitHubFileReviewResponse(BaseModel):
    """返回文件身份和已有审查结果，不包含完整源码。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    repository_url: str
    file_path: str
    review: ReviewResponse
