"""离线验证 PR 概览模型，不创建客户端或访问应用路由。"""

import json
import socket
from typing import NoReturn

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas import (
    GitHubPullRequestRequest,
    GitHubPullRequestResponse,
    validate_github_pull_number,
)

REPOSITORY_URL = "https://github.com/psf/requests"
MAX_PULL_NUMBER = 2_147_483_647
RESPONSE_FIELDS = (
    "repository_url",
    "pull_number",
    "title",
    "state",
    "draft",
    "merged",
    "html_url",
    "base_ref",
    "head_ref",
    "base_sha",
    "head_sha",
    "changed_files",
    "additions",
    "deletions",
)
INVALID_PULL_NUMBERS = [0, -1, MAX_PULL_NUMBER + 1, True, False, "1", " 1 ", 1.0, None]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """任何意外的 DNS 查询或连接都应使这些纯模型测试失败。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 编号和模型验证不应查询 DNS 或建立网络连接")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "gethostbyname", fail_network)
    monkeypatch.setattr(socket, "gethostbyname_ex", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def response_payload() -> dict[str, object]:
    """仅提供接口约定的十四个字段，不复制 GitHub 的原始响应。"""
    return {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "title": "修复请求重试",
        "state": "open",
        "draft": False,
        "merged": False,
        "html_url": "https://github.com/psf/requests/pull/42",
        "base_ref": "main",
        "head_ref": "feature/重试",
        "base_sha": "0123456789abcdef" * 2 + "01234567",
        "head_sha": "fedcba9876543210" * 2 + "fedcba98",
        "changed_files": 2,
        "additions": 10,
        "deletions": 3,
    }


@pytest.fixture(
    params=[GitHubPullRequestRequest, GitHubPullRequestResponse], ids=["request", "response"]
)
def model_and_payload(
    request: pytest.FixtureRequest, response_payload: dict[str, object]
) -> tuple[type[BaseModel], dict[str, object]]:
    """让两种模型共同遵守的输入契约使用同一组测试。"""
    model = request.param
    if model is GitHubPullRequestRequest:
        return model, {"repository_url": REPOSITORY_URL, "pull_number": 42}
    return model, response_payload


@pytest.mark.parametrize("pull_number", [1, 42, MAX_PULL_NUMBER])
def test_pull_number_valid_boundaries(pull_number: int) -> None:
    result = validate_github_pull_number(pull_number)
    assert result == pull_number
    assert type(result) is int


@pytest.mark.parametrize("pull_number", [*INVALID_PULL_NUMBERS, [], {}, b"1"])
def test_pull_number_rejects_invalid_values(pull_number: object) -> None:
    with pytest.raises(ValueError) as error:
        validate_github_pull_number(pull_number)
    assert str(error.value) == "PR 编号必须是 1–2147483647 范围内的整数。"


def test_pull_number_rejects_int_subclasses() -> None:
    class CustomInt(int):
        pass

    with pytest.raises(ValueError, match="PR 编号必须是"):
        validate_github_pull_number(CustomInt(1))


@pytest.mark.parametrize(
    "repository_url",
    [
        REPOSITORY_URL,
        "https://github.com/psf/requests/",
        "https://GITHUB.COM/Psf/Requests/",
        "https://github.com/owner/" + "r" * (2048 - len("https://github.com/owner/")),
    ],
)
def test_models_preserve_valid_repository_urls(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], repository_url: str
) -> None:
    model, payload = model_and_payload
    payload["repository_url"] = repository_url
    result = model.model_validate(payload)
    assert result.model_dump()["repository_url"] == repository_url


@pytest.mark.parametrize(
    "repository_url",
    [
        "",
        "http://github.com/psf/requests",
        "https://github.com.evil.com/psf/requests",
        "https://evil.com/psf/requests",
        "https://github.com@evil.com/psf/requests",
        "https://user:password@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://github.com/psf/requests?",
        "https://github.com/psf/requests#fragment",
        "https://github.com//requests",
        "https://github.com/psf/",
        "https://github.com/psf/requests/pull/42",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/requests//",
        " https://github.com/psf/requests",
        "https://github.com/psf/requests\n",
        "https://github.com/psf/req\x00uests",
        "https://github.com/psf\\requests",
        "https://github.com/psf/req%75ests",
        "github.com/psf/requests",
        "/psf/requests",
        "https://github.com/psf/..",
        "https://github.com/invalid_owner/requests",
        "https://github.com/psf/请求",
        "https://github.com/owner/" + "r" * (2049 - len("https://github.com/owner/")),
    ],
)
def test_models_reject_invalid_repository_urls(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], repository_url: str
) -> None:
    model, payload = model_and_payload
    payload["repository_url"] = repository_url
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("repository_url",)


@pytest.mark.parametrize(
    "repository_url", [123, True, 1.0, b"https://github.com/psf/requests", None]
)
def test_models_reject_non_string_urls(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], repository_url: object
) -> None:
    model, payload = model_and_payload
    payload["repository_url"] = repository_url
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["type"] == "string_type"


@pytest.mark.parametrize("pull_number", [1, MAX_PULL_NUMBER])
def test_models_accept_pull_number_boundaries(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: int
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    result = model.model_validate(payload)
    assert result.model_dump()["pull_number"] == pull_number


@pytest.mark.parametrize("pull_number", INVALID_PULL_NUMBERS)
def test_models_reject_invalid_pull_numbers_before_conversion(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: object
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    issue = error.value.errors()[0]
    assert issue["loc"] == ("pull_number",)
    assert issue["type"] == "value_error"
    assert "PR 编号必须是 1–2147483647 范围内的整数。" in issue["msg"]


@pytest.mark.parametrize("field", ["repository_url", "pull_number"])
def test_request_fields_are_required(field: str) -> None:
    payload: dict[str, object] = {"repository_url": REPOSITORY_URL, "pull_number": 42}
    del payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestRequest.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize(
    "field", ["token", "api_url", "ref", "branch", "body", "base", "head", "diff_url", "patch_url"]
)
def test_models_reject_extra_fields(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], field: str
) -> None:
    model, payload = model_and_payload
    payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("state", "draft", "merged"),
    [
        ("open", False, False),
        ("closed", False, False),
        ("closed", False, True),
        ("open", True, False),
    ],
    ids=["open", "closed-unmerged", "merged", "draft"],
)
def test_response_supports_pr_scenarios(
    response_payload: dict[str, object], state: str, draft: bool, merged: bool
) -> None:
    response_payload.update(state=state, draft=draft, merged=merged)
    result = GitHubPullRequestResponse.model_validate(response_payload)
    assert result.model_dump() == response_payload


def test_response_does_not_infer_or_repair_state(response_payload: dict[str, object]) -> None:
    response_payload.update(state="open", draft=True, merged=True)
    result = GitHubPullRequestResponse.model_validate(response_payload)
    assert result.state == "open"
    assert result.draft is True
    assert result.merged is True


@pytest.mark.parametrize("field", RESPONSE_FIELDS)
def test_all_response_fields_are_required(response_payload: dict[str, object], field: str) -> None:
    del response_payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", RESPONSE_FIELDS)
def test_response_rejects_null_fields(response_payload: dict[str, object], field: str) -> None:
    response_payload[field] = None
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["title", "html_url", "base_ref", "head_ref"])
@pytest.mark.parametrize("value", ["", 1, True, 1.0, b"text", [], {}])
def test_response_requires_nonempty_strings(
    response_payload: dict[str, object], field: str, value: object
) -> None:
    response_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


def test_response_preserves_unicode_and_untrimmed_strings(
    response_payload: dict[str, object],
) -> None:
    response_payload.update(title=" 修复中文标题 ", base_ref="发布/版本一", head_ref="feature/重试")
    result = GitHubPullRequestResponse.model_validate(response_payload)
    assert result.title == " 修复中文标题 "
    assert result.base_ref == "发布/版本一"
    assert result.head_ref == "feature/重试"


def test_response_html_url_is_only_a_nonempty_string(response_payload: dict[str, object]) -> None:
    response_payload.update(html_url="not-a-url", title=" ", base_ref=" ", head_ref=" ")
    result = GitHubPullRequestResponse.model_validate(response_payload)
    assert result.html_url == "not-a-url"
    assert (result.title, result.base_ref, result.head_ref) == (" ", " ", " ")


@pytest.mark.parametrize("state", ["merged", "OPEN", "Closed", "", 1, True])
def test_response_rejects_unsupported_states(
    response_payload: dict[str, object], state: object
) -> None:
    response_payload["state"] = state
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["type"] == "literal_error"


@pytest.mark.parametrize("field", ["draft", "merged"])
@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, "true", "false", "0"])
def test_response_requires_strict_booleans(
    response_payload: dict[str, object], field: str, value: object
) -> None:
    response_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "bool_type"


@pytest.mark.parametrize("field", ["changed_files", "additions", "deletions"])
@pytest.mark.parametrize("value", [0, 1, 2_147_483_648])
def test_response_accepts_nonnegative_integer_counts(
    response_payload: dict[str, object], field: str, value: int
) -> None:
    response_payload[field] = value
    result = GitHubPullRequestResponse.model_validate(response_payload)
    assert result.model_dump()[field] == value


@pytest.mark.parametrize("field", ["changed_files", "additions", "deletions"])
@pytest.mark.parametrize("value", [-1, True, False, 0.0, 1.0, "0", "1"])
def test_response_rejects_invalid_counts(
    response_payload: dict[str, object], field: str, value: object
) -> None:
    response_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["base_sha", "head_sha"])
@pytest.mark.parametrize(
    "sha",
    [
        "",
        "a" * 7,
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "a" * 39 + "G",
        "a" * 39 + "g",
        "a" * 39 + "-",
        "a" * 39 + " ",
        " " + "a" * 40,
        "a" * 40 + "\n",
        "a" * 39 + "\n",
        "a" * 19 + "\t" + "b" * 20,
        123,
        True,
        b"a" * 40,
    ],
)
def test_response_rejects_invalid_complete_shas(
    response_payload: dict[str, object], field: str, sha: object
) -> None:
    response_payload[field] = sha
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


def test_request_and_response_serialize_only_declared_fields(
    response_payload: dict[str, object],
) -> None:
    request = GitHubPullRequestRequest(repository_url=REPOSITORY_URL, pull_number=42)
    assert json.loads(request.model_dump_json()) == {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
    }
    response = GitHubPullRequestResponse.model_validate(response_payload)
    assert set(response.model_dump()) == set(RESPONSE_FIELDS)
    assert len(response.model_dump()) == 14
    assert json.loads(response.model_dump_json()) == response_payload
