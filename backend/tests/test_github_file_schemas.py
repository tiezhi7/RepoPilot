"""验证文件审查模型与纯路径校验，不创建客户端或访问外部服务。"""

import socket
from typing import NoReturn

import pytest
from pydantic import ValidationError

from app.schemas import (
    GitHubFileReviewRequest,
    GitHubFileReviewResponse,
    ReviewIssue,
    ReviewResponse,
    validate_github_file_path,
)

REPOSITORY_URL = "https://github.com/psf/requests"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型测试禁止 DNS 和网络连接，防止以后误加网络行为。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("文件路径和模型验证不应查询 DNS 或建立网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.mark.parametrize(
    "file_path",
    [
        "api.py",
        "src/requests/api.py",
        "package_name/my-file.py",
        "src/utils/__init__.py",
        ".hidden.py",
        "package..py",
        "package.v1/.hidden/module_2-test.py",
        "Src/API.py",
        ".py",
    ],
)
def test_valid_paths_are_preserved(file_path: str) -> None:
    """合法路径原样保留，包括连续点、隐藏文件名和大小写。"""
    assert validate_github_file_path(file_path) == file_path
    request = GitHubFileReviewRequest(repository_url=REPOSITORY_URL, file_path=file_path)
    assert request.model_dump() == {"repository_url": REPOSITORY_URL, "file_path": file_path}


@pytest.mark.parametrize(
    "file_path",
    [
        "",
        "../secret.py",
        "src/../secret.py",
        "./api.py",
        "src/./api.py",
        ".",
        "..",
        "/api.py",
        "//server/api.py",
        "src//api.py",
        "src/",
        "api.py/",
        "C:/temp/api.py",
        "C:api.py",
        "src\\api.py",
        "%2e%2e/secret.py",
        "src%2fapi.py",
        "src/api.py?raw=1",
        "src/api.py#fragment",
        "src/repo:api.py",
        "src/file@name.py",
        "src/file;name.py",
        "api.txt",
        "api.PY",
        "api.py.txt",
        "api",
    ],
)
def test_invalid_paths_are_rejected(file_path: str) -> None:
    """纯函数和请求模型都拒绝危险路径，而不是自动修正后接受。"""
    with pytest.raises(ValueError):
        validate_github_file_path(file_path)
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest(repository_url=REPOSITORY_URL, file_path=file_path)
    assert error.value.errors()[0]["loc"] == ("file_path",)


@pytest.mark.parametrize(
    "character", [" ", "\t", "\r", "\n", "\x00", "\x1f", "\x7f", "\u00a0", "中"]
)
@pytest.mark.parametrize("position", ["prefix", "middle", "suffix"])
def test_raw_whitespace_controls_and_unicode_are_rejected(character: str, position: str) -> None:
    """前后空白不能被 strip 掩盖，路径中的非法字符也必须被拒绝。"""
    if position == "prefix":
        file_path = character + "src/api.py"
    elif position == "middle":
        file_path = f"src/a{character}pi.py"
    else:
        file_path = "src/api.py" + character

    with pytest.raises(ValueError):
        validate_github_file_path(file_path)
    with pytest.raises(ValidationError):
        GitHubFileReviewRequest(repository_url=REPOSITORY_URL, file_path=file_path)


def test_file_path_length_boundary() -> None:
    """512 字符的合法 .py 路径通过，513 字符的路径仍保留后缀但因长度被拒绝。"""
    file_path = "a" * 509 + ".py"
    assert len(file_path) == 512
    assert validate_github_file_path(file_path) == file_path
    assert (
        GitHubFileReviewRequest(repository_url=REPOSITORY_URL, file_path=file_path).file_path
        == file_path
    )

    oversized_path = "a" + file_path
    with pytest.raises(ValueError):
        validate_github_file_path(oversized_path)
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest(repository_url=REPOSITORY_URL, file_path=oversized_path)
    assert error.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize("repository_url", [REPOSITORY_URL, "HTTPS://GitHub.Com/PsF/ReQuEsTs/"])
def test_request_preserves_the_original_repository_url(repository_url: str) -> None:
    request = GitHubFileReviewRequest(repository_url=repository_url, file_path="api.py")
    assert request.repository_url == repository_url
    assert request.file_path == "api.py"


@pytest.mark.parametrize(
    "repository_url",
    [
        "http://github.com/psf/requests",
        "https://github.com.evil.invalid/psf/requests",
        "https://github.com@evil.invalid/psf/requests",
        "https://user@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://github.com/psf/requests?raw=1",
        "https://github.com/psf/requests#fragment",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/req%75ests",
    ],
)
def test_request_reuses_repository_url_restrictions(repository_url: str) -> None:
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest(repository_url=repository_url, file_path="api.py")
    assert error.value.errors()[0]["loc"] == ("repository_url",)


def test_request_repository_url_length_boundary() -> None:
    prefix = "https://github.com/owner/"
    repository_url = prefix + "r" * (2048 - len(prefix))
    request = GitHubFileReviewRequest(repository_url=repository_url, file_path="api.py")
    assert request.repository_url == repository_url

    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest(repository_url=repository_url + "r", file_path="api.py")
    assert error.value.errors()[0]["type"] == "string_too_long"


@pytest.mark.parametrize("field", ["repository_url", "file_path"])
@pytest.mark.parametrize("value", [None, 123, True, False, 1.5, b"api.py", [], {}])
def test_request_requires_real_strings(field: str, value: object) -> None:
    payload: dict[str, object] = {"repository_url": REPOSITORY_URL, "file_path": "api.py"}
    payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "string_type"


@pytest.mark.parametrize("field", ["repository_url", "file_path"])
def test_request_requires_both_fields(field: str) -> None:
    payload = {"repository_url": REPOSITORY_URL, "file_path": "api.py"}
    del payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", ["token", "api_url", "code", "content"])
def test_request_rejects_extra_fields(field: str) -> None:
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewRequest.model_validate(
            {"repository_url": REPOSITORY_URL, "file_path": "api.py", field: "not-allowed"}
        )
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.fixture
def response_fields() -> dict[str, object]:
    """嵌套数据使用原有审查结果结构，不执行源码或调用审查器。"""
    return {
        "repository_url": REPOSITORY_URL,
        "file_path": "src/api.py",
        "review": {
            "language": "python",
            "issue_count": 1,
            "issues": [
                {"rule_id": "PY_PRINT", "severity": "info", "line": 2, "message": "发现打印调用。"}
            ],
        },
    }


def test_response_parses_existing_review_models(response_fields: dict[str, object]) -> None:
    response = GitHubFileReviewResponse.model_validate(response_fields)

    assert response.repository_url == REPOSITORY_URL
    assert response.file_path == "src/api.py"
    assert isinstance(response.review, ReviewResponse)
    assert isinstance(response.review.issues[0], ReviewIssue)
    assert response.review.language == "python"
    assert response.review.issue_count == 1
    assert response.review.issues[0].rule_id == "PY_PRINT"
    assert response.review.issues[0].line == 2
    assert response.model_dump() == response_fields
    assert set(response.model_dump()) == {"repository_url", "file_path", "review"}
    assert set(response.model_dump()["review"]) == {"language", "issue_count", "issues"}


def test_response_accepts_an_existing_review_instance() -> None:
    review = ReviewResponse(language="python", issue_count=0, issues=[])
    response = GitHubFileReviewResponse(
        repository_url=REPOSITORY_URL, file_path="api.py", review=review
    )
    assert response.review is review
    assert response.model_dump()["review"] == {"language": "python", "issue_count": 0, "issues": []}


@pytest.mark.parametrize("field", ["repository_url", "file_path"])
@pytest.mark.parametrize("value", [None, 123, True, 1.5, b"api.py", [], {}])
def test_response_requires_real_strings(
    response_fields: dict[str, object], field: str, value: object
) -> None:
    response_fields[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewResponse.model_validate(response_fields)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "string_type"


@pytest.mark.parametrize("field", ["repository_url", "file_path", "review"])
def test_response_requires_all_fields(response_fields: dict[str, object], field: str) -> None:
    del response_fields[field]
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewResponse.model_validate(response_fields)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", ["token", "code", "content"])
def test_response_rejects_extra_fields(response_fields: dict[str, object], field: str) -> None:
    response_fields[field] = "not-allowed"
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewResponse.model_validate(response_fields)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_response_keeps_existing_nested_review_validation(
    response_fields: dict[str, object],
) -> None:
    response_fields["review"] = {"language": "javascript", "issue_count": 0, "issues": []}
    with pytest.raises(ValidationError) as error:
        GitHubFileReviewResponse.model_validate(response_fields)
    assert error.value.errors()[0]["loc"] == ("review", "language")
    assert error.value.errors()[0]["type"] == "literal_error"
