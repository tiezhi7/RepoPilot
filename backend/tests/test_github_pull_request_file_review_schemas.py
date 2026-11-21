"""离线验证 PR 单文件审查契约，不查询 GitHub 或执行源码。"""

import json
import socket
from typing import NoReturn

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas import (
    GitHubPullRequestFileReviewRequest,
    GitHubPullRequestFileReviewResponse,
    ReviewResponse,
)

REPOSITORY_URL = "https://github.com/sample-owner/sample-repo"
REQUEST_FIELDS = {"repository_url", "pull_number", "file_path"}
RESPONSE_FIELDS = REQUEST_FIELDS | {"head_repository_url", "head_sha", "review"}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型的意外 DNS 查询或网络连接必须立即失败。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 单文件审查模型不应查询 DNS 或建立网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def request_payload() -> dict[str, object]:
    return {"repository_url": REPOSITORY_URL, "pull_number": 42, "file_path": "src/example.py"}


@pytest.fixture
def response_payload(request_payload: dict[str, object]) -> dict[str, object]:
    return {
        **request_payload,
        "head_repository_url": "https://github.com/contributor/sample-fork",
        "head_sha": "a1" * 20,
        "review": {"language": "python", "issue_count": 0, "issues": []},
    }


@pytest.fixture(params=["request", "response"])
def model_and_payload(
    request: pytest.FixtureRequest,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> tuple[type[BaseModel], dict[str, object]]:
    if request.param == "request":
        return GitHubPullRequestFileReviewRequest, request_payload
    return GitHubPullRequestFileReviewResponse, response_payload


def test_request_has_exactly_three_fields(request_payload: dict[str, object]) -> None:
    result = GitHubPullRequestFileReviewRequest.model_validate(request_payload)
    assert result.model_dump() == request_payload
    assert set(result.model_dump()) == REQUEST_FIELDS


@pytest.mark.parametrize("same_repository", [True, False])
def test_response_preserves_same_repository_or_fork(
    response_payload: dict[str, object], same_repository: bool
) -> None:
    if same_repository:
        response_payload["head_repository_url"] = REPOSITORY_URL
    result = GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert set(result.model_dump()) == RESPONSE_FIELDS
    assert result.model_dump() == response_payload
    assert isinstance(result.review, ReviewResponse)


@pytest.mark.parametrize("pull_number", [1, 2_147_483_647])
def test_models_accept_pull_number_boundaries(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: int
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    result = model.model_validate(payload).model_dump()["pull_number"]
    assert type(result) is int
    assert result == pull_number


@pytest.mark.parametrize("pull_number", [0, -1, 2_147_483_648, True, False, 42.0, "42", None])
def test_models_reject_invalid_pull_numbers(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: object
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("pull_number",)


def test_models_reuse_strict_pull_number_helper(
    model_and_payload: tuple[type[BaseModel], dict[str, object]],
) -> None:
    class CustomInt(int):
        pass

    model, payload = model_and_payload
    payload["pull_number"] = CustomInt(42)
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("pull_number",)


@pytest.mark.parametrize("field", ["repository_url", "head_repository_url"])
def test_urls_preserve_valid_original_strings(
    response_payload: dict[str, object], field: str
) -> None:
    original = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
    response_payload[field] = original
    result = GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert result.model_dump()[field] == original
    if field == "repository_url":
        request = {key: response_payload[key] for key in REQUEST_FIELDS}
        assert GitHubPullRequestFileReviewRequest.model_validate(request).repository_url == original


@pytest.mark.parametrize("field", ["repository_url", "head_repository_url"])
@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/owner/repo",
        "https://github.com.evil.test/owner/repo",
        "https://user:secret@github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/repo/pull/42",
        "https://github.com/owner/repo?ref=main",
        "https://github.com/owner/repo#",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/re%70o",
        "https://github.com/owner/repo\n",
        "https://github.com/owner/仓库",
        "https://github.com/owner/" + "a" * 2048,
        "",
        None,
        42,
        b"https://github.com/owner/repo",
    ],
)
def test_response_validates_both_repository_urls(
    response_payload: dict[str, object], field: str, value: object
) -> None:
    response_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize(
    "value", ["https://github.com.evil.test/owner/repo", "", None, 42, b"https://github.com/o/r"]
)
def test_request_validates_repository_url(
    request_payload: dict[str, object], value: object
) -> None:
    request_payload["repository_url"] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == ("repository_url",)


@pytest.mark.parametrize(
    "file_path", ["example.py", "Src/Example_2-test.py", "src/package..py", "a" * 509 + ".py"]
)
def test_models_preserve_valid_paths(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], file_path: str
) -> None:
    model, payload = model_and_payload
    payload["file_path"] = file_path
    assert model.model_validate(payload).model_dump()["file_path"] == file_path


@pytest.mark.parametrize(
    "file_path",
    [
        "",
        "a" * 510 + ".py",
        "/src/example.py",
        "src//example.py",
        "./example.py",
        "src/../example.py",
        "src\\example.py",
        "src/%65xample.py",
        "src/example.PY",
        "src/example.txt",
        "src/示例.py",
        "src/my file.py",
        "src/example.py\n",
        "src/example.py?ref=main",
        None,
        42,
        b"example.py",
    ],
)
def test_models_reject_unsafe_or_non_python_paths(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], file_path: object
) -> None:
    model, payload = model_and_payload
    payload["file_path"] = file_path
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("file_path",)


@pytest.mark.parametrize("field", sorted(REQUEST_FIELDS))
def test_request_fields_are_required(request_payload: dict[str, object], field: str) -> None:
    del request_payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", sorted(RESPONSE_FIELDS))
def test_response_fields_are_required(response_payload: dict[str, object], field: str) -> None:
    del response_payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", sorted(RESPONSE_FIELDS))
def test_response_fields_cannot_be_null(response_payload: dict[str, object], field: str) -> None:
    response_payload[field] = None
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["token", "api_url", "ref", "branch", "page", "code", "patch"])
def test_models_reject_extra_fields(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], field: str
) -> None:
    model, payload = model_and_payload
    payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("field", ["head_repository_url", "head_sha", "review"])
def test_caller_cannot_choose_resolved_source(
    request_payload: dict[str, object], field: str
) -> None:
    request_payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("head_sha", ["0" * 40, "0123456789abcdef" * 2 + "01234567"])
def test_response_preserves_full_lowercase_sha(
    response_payload: dict[str, object], head_sha: str
) -> None:
    response_payload["head_sha"] = head_sha
    assert GitHubPullRequestFileReviewResponse.model_validate(response_payload).head_sha == head_sha


@pytest.mark.parametrize(
    "head_sha",
    [
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 39 + "\n",
        "a" * 40 + "\n",
        " " + "a" * 39,
        "a" * 39 + " ",
        "a" * 39 + "\x00",
        "ａ" * 40,
        "a" * 39 + "\ud800",
        123,
        True,
        b"a" * 40,
        [],
    ],
)
def test_response_rejects_invalid_sha_without_normalizing(
    response_payload: dict[str, object], head_sha: object
) -> None:
    response_payload["head_sha"] = head_sha
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("head_sha",)


@pytest.mark.parametrize(
    ("field", "value", "location"),
    [
        ("language", "javascript", ("review", "language")),
        ("issue_count", -1, ("review", "issue_count")),
        ("issues", "invalid", ("review", "issues")),
    ],
)
def test_nested_review_uses_existing_validation(
    response_payload: dict[str, object], field: str, value: object, location: tuple[str, ...]
) -> None:
    response_payload["review"] = {
        "language": "python",
        "issue_count": 0,
        "issues": [],
        field: value,
    }
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == location


def test_response_reuses_review_instance_and_round_trips_unicode(
    response_payload: dict[str, object],
) -> None:
    review = ReviewResponse.model_validate(
        {
            "language": "python",
            "issue_count": 1,
            "issues": [
                {"rule_id": "PY_SYNTAX", "severity": "error", "line": 3, "message": "语法错误 🙂"}
            ],
        }
    )
    response_payload["review"] = review
    result = GitHubPullRequestFileReviewResponse.model_validate(response_payload)
    assert result.review is review
    encoded = result.model_dump_json()
    assert json.loads(encoded)["review"] == review.model_dump()
    assert GitHubPullRequestFileReviewResponse.model_validate_json(encoded) == result


def test_models_publish_required_fields_and_constraints() -> None:
    request_schema = GitHubPullRequestFileReviewRequest.model_json_schema()
    response_schema = GitHubPullRequestFileReviewResponse.model_json_schema()
    assert set(request_schema["properties"]) == set(request_schema["required"]) == REQUEST_FIELDS
    assert set(response_schema["properties"]) == set(response_schema["required"]) == RESPONSE_FIELDS
    assert request_schema["additionalProperties"] is False
    assert response_schema["additionalProperties"] is False
    assert request_schema["properties"]["pull_number"]["minimum"] == 1
    assert request_schema["properties"]["pull_number"]["maximum"] == 2_147_483_647
    assert request_schema["properties"]["file_path"]["maxLength"] == 512
    assert response_schema["properties"]["head_sha"]["minLength"] == 40
    assert response_schema["properties"]["head_sha"]["maxLength"] == 40
    assert response_schema["properties"]["review"] == {"$ref": "#/$defs/ReviewResponse"}
