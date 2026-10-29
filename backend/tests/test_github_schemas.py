"""验证 GitHub 模型和纯 URL 校验；不访问网络，不执行输入中的代码。"""

import socket
from typing import NoReturn

import pytest
from pydantic import ValidationError

from app.schemas import (
    GitHubRepositoryRequest,
    GitHubRepositoryResponse,
    parse_github_repository_url,
)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件所有测试禁止 DNS 查询和建立网络连接。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("模型和 URL 校验不应查询 DNS 或建立网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/psf/requests", ("psf", "requests")),
        ("https://github.com/psf/requests/", ("psf", "requests")),
        ("https://GitHub.Com/psf/requests", ("psf", "requests")),
        ("HTTPS://GITHUB.COM/PsF/ReQuEsTs", ("PsF", "ReQuEsTs")),
        ("https://github.com/Example-Owner/Repo_1.2-name", ("Example-Owner", "Repo_1.2-name")),
        ("https://github.com/user/.github", ("user", ".github")),
        ("https://github.com/a/0", ("a", "0")),
    ],
)
def test_valid_repository_urls(url: str, expected: tuple[str, str]) -> None:
    """合法地址得到准确的路径段，并保留 owner、repo 的大小写。"""
    assert parse_github_repository_url(url) == expected
    request = GitHubRepositoryRequest(repository_url=url)
    assert request.model_dump() == {"repository_url": url}


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/psf/requests",
        "ftp://github.com/psf/requests",
        "ssh://github.com/psf/requests",
        "github.com/psf/requests",
        "/psf/requests",
        "//github.com/psf/requests",
        "https://github.com.evil.com/psf/requests",
        "https://evil.com/psf/requests",
        "https://github.com@evil.com/psf/requests",
        "https://www.github.com/psf/requests",
        "https://github.com./psf/requests",
        "https://127.0.0.1/psf/requests",
        "https://[::1]/psf/requests",
        "https://user@github.com/psf/requests",
        "https://user:password@github.com/psf/requests",
        "https://:password@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://github.com:80/psf/requests",
        "https://github.com:invalid/psf/requests",
        "https://github.com:/psf/requests",
        "https://github.com/psf/requests?tab=readme",
        "https://github.com/psf/requests?",
        "https://github.com/psf/requests#readme",
        "https://github.com/psf/requests#",
        "https://github.com",
        "https://github.com/psf",
        "https://github.com//requests",
        "https://github.com/psf/",
        "https://github.com/psf/requests//",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf/requests/blob/main/file.py",
        "https://github.com/psf/requests/pull/1",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests.GIT/",
        "https://github.com/psf/req%75ests",
        "https://github.com/%70sf/requests",
        "https://github.com/psf/requests%2f..%2fprivate",
        "https://github.com/psf\\requests",
        "https://github.com\\@evil.com/psf/requests",
        "https://github.com/./requests",
        "https://github.com/../requests",
        "https://github.com/psf/.",
        "https://github.com/psf/..",
        "https://github.com/bad_owner/requests",
        "https://github.com/-owner/requests",
        "https://github.com/owner-/requests",
        "https://github.com/owner--name/requests",
        "https://github.com/用户/requests",
        "https://github.com/psf/仓库",
        "https://github.com/psf/repo:name",
        "https://github.com/psf/repo;params",
        "https://github.com/psf/repo@name",
        "https://[github.com/psf/requests",
        "https://github.com\uff1a443/psf/requests",
    ],
)
def test_invalid_repository_urls_are_rejected(url: str) -> None:
    """纯函数和请求模型都拒绝不符合约定的地址，包括本地主机地址。"""
    with pytest.raises(ValueError):
        parse_github_repository_url(url)
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryRequest(repository_url=url)
    assert error.value.errors()[0]["loc"] == ("repository_url",)


@pytest.mark.parametrize(
    "character", [" ", "\t", "\r", "\n", "\x00", "\x1f", "\x7f", "\x85", "\u00a0"]
)
@pytest.mark.parametrize("position", ["prefix", "hostname", "suffix"])
def test_raw_whitespace_and_controls_are_rejected(character: str, position: str) -> None:
    """即使 URL 解析器可能移除某些字符，也不能把它们当成合法输入。"""
    if position == "prefix":
        url = character + "https://github.com/psf/requests"
    elif position == "hostname":
        url = f"https://git{character}hub.com/psf/requests"
    else:
        url = "https://github.com/psf/requests" + character

    with pytest.raises(ValueError):
        parse_github_repository_url(url)
    with pytest.raises(ValidationError):
        GitHubRepositoryRequest(repository_url=url)


def test_repository_url_length_boundary() -> None:
    """2048 字符通过本地格式校验，2049 字符被拒绝；不判断仓库是否存在。"""
    prefix = "https://github.com/owner/"
    repo = "r" * (2048 - len(prefix))
    url = prefix + repo
    assert len(url) == 2048
    assert parse_github_repository_url(url) == ("owner", repo)
    assert GitHubRepositoryRequest(repository_url=url).repository_url == url

    with pytest.raises(ValueError):
        parse_github_repository_url(url + "r")
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryRequest(repository_url=url + "r")
    assert error.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize(
    "value", [None, 123, True, False, 1.5, b"https://github.com/psf/requests", [], {}]
)
def test_request_requires_a_real_string(value: object) -> None:
    """错误类型不得被自动转换为 URL 字符串。"""
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryRequest.model_validate({"repository_url": value})
    assert error.value.errors()[0]["type"] == "string_type"


def test_request_requires_repository_url() -> None:
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryRequest.model_validate({})
    assert error.value.errors()[0]["loc"] == ("repository_url",)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("extra_field", ["token", "api_url"])
def test_request_rejects_extra_fields(extra_field: str) -> None:
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryRequest.model_validate(
            {"repository_url": "https://github.com/psf/requests", extra_field: "not-allowed"}
        )
    assert error.value.errors()[0]["loc"] == (extra_field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_parsed_parts_build_only_the_fixed_api_path() -> None:
    """只拼接字符串验证路径契约，不创建客户端或发送 HTTP 请求。"""
    owner, repo = parse_github_repository_url("https://GitHub.Com/Example-Owner/Repo_1.2/")
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    assert api_url == "https://api.github.com/repos/Example-Owner/Repo_1.2"


def test_input_code_is_not_executed(tmp_path) -> None:
    """输入即使是一段会写文件的 Python 代码，也只能作为非法 URL 被拒绝。"""
    target = tmp_path / "url-validation-must-not-write.txt"
    code = f"__import__('pathlib').Path({str(target)!r}).write_text('unexpected')"
    assert not target.exists()

    with pytest.raises(ValueError):
        parse_github_repository_url(code)
    with pytest.raises(ValidationError):
        GitHubRepositoryRequest(repository_url=code)

    assert not target.exists()


@pytest.fixture
def repository_fields() -> dict[str, object]:
    """这些是固定测试数据，不代表 GitHub 实时仓库数据。"""
    return {
        "full_name": "sample-owner/sample-repo",
        "description": "示例仓库",
        "language": "Python",
        "stars": 12,
        "default_branch": "main",
        "archived": False,
        "html_url": "https://github.com/sample-owner/sample-repo",
    }


@pytest.mark.parametrize(("stars", "archived"), [(0, False), (12, True)])
def test_response_preserves_all_fields(
    repository_fields: dict[str, object], stars: int, archived: bool
) -> None:
    repository_fields.update(stars=stars, archived=archived)
    response = GitHubRepositoryResponse.model_validate(repository_fields)
    assert response.model_dump() == repository_fields


@pytest.mark.parametrize(
    ("description", "language"), [(None, None), (None, "Python"), ("示例仓库", None)]
)
def test_response_allows_null_description_and_language(
    repository_fields: dict[str, object], description: str | None, language: str | None
) -> None:
    repository_fields.update(description=description, language=language)
    response = GitHubRepositoryResponse.model_validate(repository_fields)
    assert response.description == description
    assert response.language == language
    assert response.model_dump() == repository_fields


def test_response_rejects_negative_stars(repository_fields: dict[str, object]) -> None:
    repository_fields["stars"] = -1
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryResponse.model_validate(repository_fields)
    assert error.value.errors()[0]["loc"] == ("stars",)
    assert error.value.errors()[0]["type"] == "greater_than_equal"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_name", 123),
        ("full_name", b"sample-owner/sample-repo"),
        ("description", 123),
        ("description", b"description"),
        ("language", True),
        ("language", b"Python"),
        ("stars", "12"),
        ("stars", 12.0),
        ("stars", True),
        ("stars", None),
        ("default_branch", 123),
        ("default_branch", b"main"),
        ("archived", "false"),
        ("archived", 0),
        ("archived", 1),
        ("archived", None),
        ("html_url", 123),
        ("html_url", b"https://github.com/sample-owner/sample-repo"),
    ],
)
def test_response_rejects_wrong_types(
    repository_fields: dict[str, object], field: str, value: object
) -> None:
    repository_fields[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryResponse.model_validate(repository_fields)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    "field",
    ["full_name", "description", "language", "stars", "default_branch", "archived", "html_url"],
)
def test_response_requires_every_field(repository_fields: dict[str, object], field: str) -> None:
    """允许 null 不代表字段可以省略，包括 description 和 language。"""
    repository_fields.pop(field)
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryResponse.model_validate(repository_fields)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", ["id", "private", "stargazers_count"])
def test_response_rejects_extra_fields(repository_fields: dict[str, object], field: str) -> None:
    repository_fields[field] = 1
    with pytest.raises(ValidationError) as error:
        GitHubRepositoryResponse.model_validate(repository_fields)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"
