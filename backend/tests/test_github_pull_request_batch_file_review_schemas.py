"""离线验证 PR 指定多文件审查的数据契约，不创建客户端或调用应用。"""

import json
import socket
from typing import NoReturn

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas import (
    GitHubPullRequestBatchFileReviewItem,
    GitHubPullRequestBatchFileReviewRequest,
    GitHubPullRequestBatchFileReviewResponse,
    ReviewResponse,
    validate_github_pull_request_file_paths,
)

REPOSITORY_URL = "https://github.com/example/repo"
HEAD_REPOSITORY_URL = "https://github.com/contributor/fork"
HEAD_SHA = "0123456789abcdef" * 2 + "01234567"
REQUEST_FIELDS = {"repository_url", "pull_number", "file_paths"}
RESPONSE_FIELDS = {"repository_url", "pull_number", "head_repository_url", "head_sha", "files"}
ITEM_FIELDS = {"file_path", "review"}
INVALID_PATHS = [
    "",
    "a" * 510 + ".py",
    "/src/a.py",
    "./a.py",
    "src/../a.py",
    "src//a.py",
    "src\\a.py",
    "src/%61.py",
    "src/a.PY",
    "src/a.txt",
    "src/示例.py",
    "src/my file.py",
    "src/a.py\n",
    "src/a.py?ref=main",
    None,
    True,
    1,
    b"a.py",
    [],
    {},
]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯模型与路径校验不应发起 DNS 查询或网络连接。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("批量审查模型不应查询 DNS 或建立网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


def file_result(file_path: str) -> dict[str, object]:
    return {
        "file_path": file_path,
        "review": {"language": "python", "issue_count": 0, "issues": []},
    }


@pytest.fixture
def request_payload() -> dict[str, object]:
    return {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "file_paths": ["src/z.py", "src/a.py"],
    }


@pytest.fixture
def response_payload() -> dict[str, object]:
    return {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "head_repository_url": HEAD_REPOSITORY_URL,
        "head_sha": HEAD_SHA,
        "files": [file_result("src/z.py"), file_result("src/a.py")],
    }


@pytest.fixture(params=["request", "response", "item"])
def model_and_payload(
    request: pytest.FixtureRequest,
    request_payload: dict[str, object],
    response_payload: dict[str, object],
) -> tuple[type[BaseModel], dict[str, object], set[str]]:
    if request.param == "request":
        return GitHubPullRequestBatchFileReviewRequest, request_payload, REQUEST_FIELDS
    if request.param == "response":
        return GitHubPullRequestBatchFileReviewResponse, response_payload, RESPONSE_FIELDS
    return GitHubPullRequestBatchFileReviewItem, file_result("src/a.py"), ITEM_FIELDS


@pytest.mark.parametrize(
    "paths",
    [
        ["a.py"],
        ["src/z.py", "src/a.py"],
        ["src/a.py", "src/A.py", "src/package..py"],
        ["a" * 509 + ".py", "Src/Example_2-test.py", ".hidden/module.py"],
    ],
)
def test_helper_and_models_preserve_valid_path_order(
    request_payload: dict[str, object], response_payload: dict[str, object], paths: list[str]
) -> None:
    original = paths.copy()
    validated = validate_github_pull_request_file_paths(paths)
    assert validated == paths == original
    assert validated is not paths
    request_payload["file_paths"] = paths
    response_payload["files"] = [file_result(path) for path in paths]
    request = GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    response = GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert request.file_paths == original
    assert [item.file_path for item in response.files] == original
    validated.append("other.py")
    assert paths == original
    assert request.file_paths == original


@pytest.mark.parametrize("value", [None, "a.py", b"a.py", ("a.py",), {"a.py"}, {"a": "a.py"}])
def test_helper_and_request_require_a_list(
    request_payload: dict[str, object], value: object
) -> None:
    with pytest.raises(ValueError):
        validate_github_pull_request_file_paths(value)
    request_payload["file_paths"] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == ("file_paths",)


@pytest.mark.parametrize("paths", [[], ["a.py", "b.py", "c.py", "d.py"]])
def test_helper_and_models_reject_file_count_outside_quota(
    request_payload: dict[str, object], response_payload: dict[str, object], paths: list[str]
) -> None:
    with pytest.raises(ValueError):
        validate_github_pull_request_file_paths(paths)
    request_payload["file_paths"] = paths
    response_payload["files"] = [file_result(path) for path in paths]
    with pytest.raises(ValidationError) as request_error:
        GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    with pytest.raises(ValidationError) as response_error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert request_error.value.errors()[0]["loc"] == ("file_paths",)
    assert response_error.value.errors()[0]["loc"] == ("files",)


@pytest.mark.parametrize("paths", [["a.py", "a.py"], ["b.py", "a.py", "b.py"]])
def test_duplicate_paths_are_rejected_without_silent_deduplication(
    request_payload: dict[str, object], response_payload: dict[str, object], paths: list[str]
) -> None:
    original = paths.copy()
    with pytest.raises(ValueError, match="不能重复"):
        validate_github_pull_request_file_paths(paths)
    request_payload["file_paths"] = paths
    response_payload["files"] = [file_result(path) for path in paths]
    with pytest.raises(ValidationError) as request_error:
        GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    with pytest.raises(ValidationError) as response_error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert request_error.value.errors()[0]["loc"] == ("file_paths",)
    assert response_error.value.errors()[0]["loc"] == ("files",)
    assert paths == original


@pytest.mark.parametrize("value", INVALID_PATHS)
@pytest.mark.parametrize("position", [0, 2])
def test_all_request_paths_are_validated_before_use(
    request_payload: dict[str, object], value: object, position: int
) -> None:
    paths = ["a.py", "b.py", "c.py"]
    paths[position] = value
    with pytest.raises(ValueError):
        validate_github_pull_request_file_paths(paths)
    request_payload["file_paths"] = paths
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == ("file_paths",)


@pytest.mark.parametrize("value", INVALID_PATHS)
def test_response_validates_each_item_path(
    response_payload: dict[str, object], value: object
) -> None:
    invalid = file_result("placeholder.py")
    invalid["file_path"] = value
    response_payload["files"] = [file_result("a.py"), invalid]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("files", 1, "file_path")


def test_models_require_every_declared_field(
    model_and_payload: tuple[type[BaseModel], dict[str, object], set[str]],
) -> None:
    model, payload, fields = model_and_payload
    assert set(payload) == fields
    for field in fields:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ValidationError) as error:
            model.model_validate(incomplete)
        assert error.value.errors()[0]["loc"] == (field,)
        assert error.value.errors()[0]["type"] == "missing"


@pytest.mark.parametrize("field", ["token", "api_url", "ref", "branch", "page", "code", "patch"])
def test_models_reject_unexpected_fields(
    model_and_payload: tuple[type[BaseModel], dict[str, object], set[str]], field: str
) -> None:
    model, payload, _ = model_and_payload
    payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("field", ["head_repository_url", "head_sha", "review", "files"])
def test_request_cannot_choose_resolved_source_or_results(
    request_payload: dict[str, object], field: str
) -> None:
    request_payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    assert error.value.errors()[0]["loc"] == (field,)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("number", [1, 2_147_483_647])
def test_request_and_response_accept_pull_number_boundaries(
    request_payload: dict[str, object], response_payload: dict[str, object], number: int
) -> None:
    for model, payload in (
        (GitHubPullRequestBatchFileReviewRequest, request_payload),
        (GitHubPullRequestBatchFileReviewResponse, response_payload),
    ):
        payload["pull_number"] = number
        result = model.model_validate(payload).pull_number
        assert type(result) is int
        assert result == number


@pytest.mark.parametrize("number", [0, -1, 2_147_483_648, True, False, "42", 42.0, None])
def test_request_and_response_reject_non_strict_pull_numbers(
    request_payload: dict[str, object], response_payload: dict[str, object], number: object
) -> None:
    for model, payload in (
        (GitHubPullRequestBatchFileReviewRequest, request_payload),
        (GitHubPullRequestBatchFileReviewResponse, response_payload),
    ):
        payload["pull_number"] = number
        with pytest.raises(ValidationError) as error:
            model.model_validate(payload)
        assert error.value.errors()[0]["loc"] == ("pull_number",)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://github.com.evil.test/owner/repo",
        "https://user:secret@github.com/owner/repo",
        "https://github.com:443/owner/repo",
        "https://github.com/owner/repo?ref=main",
        "https://github.com/owner/repo#",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/仓库",
        "https://github.com/owner/" + "a" * 2048,
        "",
        None,
        42,
        b"https://github.com/o/r",
    ],
)
def test_all_repository_fields_reuse_url_validation(
    request_payload: dict[str, object], response_payload: dict[str, object], url: object
) -> None:
    for model, payload, field in (
        (GitHubPullRequestBatchFileReviewRequest, request_payload, "repository_url"),
        (GitHubPullRequestBatchFileReviewResponse, response_payload, "repository_url"),
        (GitHubPullRequestBatchFileReviewResponse, response_payload, "head_repository_url"),
    ):
        candidate = {**payload, field: url}
        with pytest.raises(ValidationError) as error:
            model.model_validate(candidate)
        assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("same_repository", [True, False])
def test_urls_preserve_valid_spelling_and_source_repository(
    request_payload: dict[str, object], response_payload: dict[str, object], same_repository: bool
) -> None:
    original = "https://GITHUB.COM/Sample-Owner/Sample-Repo/"
    request_payload["repository_url"] = original
    response_payload["repository_url"] = original
    if same_repository:
        response_payload["head_repository_url"] = original
    request = GitHubPullRequestBatchFileReviewRequest.model_validate(request_payload)
    response = GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert request.repository_url == response.repository_url == original
    assert response.head_repository_url == (original if same_repository else HEAD_REPOSITORY_URL)


@pytest.mark.parametrize(
    "value",
    [
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 40 + "\n",
        "a" * 39 + "\n",
        "ａ" * 40,
        None,
        42,
        True,
        b"a" * 40,
    ],
)
def test_response_rejects_invalid_sha_without_normalizing(
    response_payload: dict[str, object], value: object
) -> None:
    response_payload["head_sha"] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("head_sha",)


@pytest.mark.parametrize("sha", ["0" * 40, HEAD_SHA])
def test_response_preserves_full_lowercase_sha(
    response_payload: dict[str, object], sha: str
) -> None:
    response_payload["head_sha"] = sha
    assert GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload).head_sha == sha


@pytest.mark.parametrize("value", [None, "invalid", {}, (file_result("a.py"),), [None]])
def test_response_rejects_malformed_result_lists(
    response_payload: dict[str, object], value: object
) -> None:
    response_payload["files"] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"][0] == "files"


def test_response_cannot_mix_per_file_sources_or_partial_errors(
    response_payload: dict[str, object],
) -> None:
    for field in ("head_sha", "head_repository_url", "error", "status"):
        response_payload["files"] = [{**file_result("a.py"), field: "unexpected"}]
        with pytest.raises(ValidationError) as error:
            GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
        assert error.value.errors()[0]["loc"] == ("files", 0, field)
        assert error.value.errors()[0]["type"] == "extra_forbidden"
    response_payload["files"] = [{"file_path": "a.py", "review": None}]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("files", 0, "review")


@pytest.mark.parametrize(
    ("field", "value"),
    [("language", "javascript"), ("issue_count", -1), ("issues", "invalid")],
)
def test_nested_reviews_keep_existing_validation(
    response_payload: dict[str, object], field: str, value: object
) -> None:
    response_payload["files"] = [
        {
            "file_path": "a.py",
            "review": {
                "language": "python",
                "issue_count": 0,
                "issues": [],
                field: value,
            },
        }
    ]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("files", 0, "review", field)


def test_response_reuses_review_instances_and_round_trips_unicode(
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
    response_payload["files"] = [{"file_path": "b.py", "review": review}, file_result("a.py")]
    result = GitHubPullRequestBatchFileReviewResponse.model_validate(response_payload)
    assert set(result.model_dump()) == RESPONSE_FIELDS
    assert [item.file_path for item in result.files] == ["b.py", "a.py"]
    assert result.files[0].review is review
    assert set(result.files[0].model_dump()) == ITEM_FIELDS
    encoded = result.model_dump_json()
    assert json.loads(encoded)["files"][0]["review"] == review.model_dump()
    assert GitHubPullRequestBatchFileReviewResponse.model_validate_json(encoded) == result


def test_json_schema_publishes_exact_contract_and_limits() -> None:
    request = GitHubPullRequestBatchFileReviewRequest.model_json_schema()
    response = GitHubPullRequestBatchFileReviewResponse.model_json_schema()
    item = GitHubPullRequestBatchFileReviewItem.model_json_schema()
    for schema, fields in (
        (request, REQUEST_FIELDS),
        (response, RESPONSE_FIELDS),
        (item, ITEM_FIELDS),
    ):
        assert set(schema["properties"]) == set(schema["required"]) == fields
        assert schema["additionalProperties"] is False
    for schema, field in ((request, "file_paths"), (response, "files")):
        assert schema["properties"][field]["minItems"] == 1
        assert schema["properties"][field]["maxItems"] == 3
    assert request["properties"]["file_paths"]["items"] == {"type": "string"}
    assert response["properties"]["files"]["items"] == {
        "$ref": "#/$defs/GitHubPullRequestBatchFileReviewItem"
    }
    assert item["properties"]["file_path"]["maxLength"] == 512
    assert item["properties"]["review"] == {"$ref": "#/$defs/ReviewResponse"}
    assert response["properties"]["head_sha"]["minLength"] == 40
    assert response["properties"]["head_sha"]["maxLength"] == 40
