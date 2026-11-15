"""离线验证 PR 文件列表模型，文件名只作为元数据保留。"""

import json
import socket
from typing import NoReturn

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas import (
    GitHubPullRequestFile,
    GitHubPullRequestFilesRequest,
    GitHubPullRequestFilesResponse,
    validate_github_pull_request_files_page,
)

REPOSITORY_URL = "https://github.com/psf/requests"
INVALID_PAGES = [0, -1, 101, True, False, 1.0, "1", " 1 ", None, b"1", [], {}]
FILE_FIELDS = {"filename", "status", "additions", "deletions", "changes", "previous_filename"}
RESPONSE_FIELDS = {"repository_url", "pull_number", "page", "per_page", "files"}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯模型测试中的意外 DNS 查询或网络连接应立即失败。"""

    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("PR 文件列表模型不应查询 DNS 或建立网络连接")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


@pytest.fixture
def file_payload() -> dict[str, object]:
    return {
        "filename": "文档/使用说明.md",
        "status": "modified",
        "additions": 4,
        "deletions": 2,
        "changes": 6,
    }


@pytest.fixture
def response_payload(file_payload: dict[str, object]) -> dict[str, object]:
    return {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "page": 1,
        "per_page": 30,
        "files": [file_payload],
    }


@pytest.fixture(
    params=[GitHubPullRequestFilesRequest, GitHubPullRequestFilesResponse],
    ids=["request", "response"],
)
def model_and_payload(
    request: pytest.FixtureRequest, response_payload: dict[str, object]
) -> tuple[type[BaseModel], dict[str, object]]:
    if request.param is GitHubPullRequestFilesRequest:
        return request.param, {"repository_url": REPOSITORY_URL, "pull_number": 42}
    return request.param, response_payload


@pytest.mark.parametrize("page", [1, 2, 100])
def test_page_helper_accepts_boundaries(page: int) -> None:
    result = validate_github_pull_request_files_page(page)
    assert result == page
    assert type(result) is int


@pytest.mark.parametrize("page", INVALID_PAGES)
def test_page_helper_rejects_invalid_values(page: object) -> None:
    with pytest.raises(ValueError) as error:
        validate_github_pull_request_files_page(page)
    assert str(error.value) == "PR 文件列表页码必须是 1–100 范围内的整数。"


def test_page_helper_and_models_reject_int_subclass(response_payload: dict[str, object]) -> None:
    class CustomInt(int):
        pass

    with pytest.raises(ValueError):
        validate_github_pull_request_files_page(CustomInt(1))
    for model in (GitHubPullRequestFilesRequest, GitHubPullRequestFilesResponse):
        payload = response_payload.copy()
        if model is GitHubPullRequestFilesRequest:
            payload = {"repository_url": REPOSITORY_URL, "pull_number": 42}
        payload["page"] = CustomInt(1)
        with pytest.raises(ValidationError) as error:
            model.model_validate(payload)
        assert error.value.errors()[0]["loc"] == ("page",)


def test_request_defaults_only_page() -> None:
    result = GitHubPullRequestFilesRequest(repository_url=REPOSITORY_URL, pull_number=42)
    assert result.model_dump() == {
        "repository_url": REPOSITORY_URL,
        "pull_number": 42,
        "page": 1,
    }


@pytest.mark.parametrize("page", [1, 100])
def test_models_accept_page_boundaries(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], page: int
) -> None:
    model, payload = model_and_payload
    payload["page"] = page
    assert model.model_validate(payload).model_dump()["page"] == page


@pytest.mark.parametrize("page", INVALID_PAGES)
def test_models_reject_page_before_coercion(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], page: object
) -> None:
    model, payload = model_and_payload
    payload["page"] = page
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("page",)


@pytest.mark.parametrize("repository_url", [REPOSITORY_URL, "https://GITHUB.COM/Psf/Requests/"])
def test_models_preserve_repository_url(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], repository_url: str
) -> None:
    model, payload = model_and_payload
    payload["repository_url"] = repository_url
    assert model.model_validate(payload).model_dump()["repository_url"] == repository_url


@pytest.mark.parametrize(
    "repository_url",
    [
        "http://github.com/psf/requests",
        "https://github.com.evil.com/psf/requests",
        "https://user:secret@github.com/psf/requests",
        "https://github.com:443/psf/requests",
        "https://github.com/psf/requests/pull/42",
        "https://github.com/psf/requests?",
        "https://github.com/psf/requests#",
        "https://github.com/psf/requests.git",
        "https://github.com/psf/req%75ests",
        "https://github.com/psf/requests\n",
        "https://github.com/psf/请求",
        "https://github.com/owner/" + "r" * 2048,
        "",
        None,
        42,
        b"https://github.com/psf/requests",
    ],
)
def test_models_reuse_strict_repository_validation(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], repository_url: object
) -> None:
    model, payload = model_and_payload
    payload["repository_url"] = repository_url
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("repository_url",)


@pytest.mark.parametrize("pull_number", [1, 2_147_483_647])
def test_models_accept_pull_number_boundaries(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: int
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    assert model.model_validate(payload).model_dump()["pull_number"] == pull_number


@pytest.mark.parametrize("pull_number", [0, -1, 2_147_483_648, True, False, 1.0, "1", None])
def test_models_reuse_strict_pull_number_validation(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], pull_number: object
) -> None:
    model, payload = model_and_payload
    payload["pull_number"] = pull_number
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["loc"] == ("pull_number",)


@pytest.mark.parametrize("field", ["repository_url", "pull_number"])
def test_request_requires_identity_fields(field: str) -> None:
    payload: dict[str, object] = {"repository_url": REPOSITORY_URL, "pull_number": 42}
    del payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesRequest.model_validate(payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", sorted(RESPONSE_FIELDS))
def test_response_requires_all_five_fields(response_payload: dict[str, object], field: str) -> None:
    del response_payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("per_page", [0, 29, 31, 100, 30.0, "30", True, False, None])
def test_response_requires_strict_fixed_per_page(
    response_payload: dict[str, object], per_page: object
) -> None:
    response_payload["per_page"] = per_page
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("per_page",)


@pytest.mark.parametrize("field", ["token", "api_url", "has_next"])
def test_models_forbid_extra_fields(
    model_and_payload: tuple[type[BaseModel], dict[str, object]], field: str
) -> None:
    model, payload = model_and_payload
    payload[field] = "unexpected"
    with pytest.raises(ValidationError) as error:
        model.model_validate(payload)
    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_request_cannot_override_per_page() -> None:
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesRequest.model_validate(
            {"repository_url": REPOSITORY_URL, "pull_number": 42, "per_page": 100}
        )
    assert error.value.errors()[0]["loc"] == ("per_page",)


@pytest.mark.parametrize("count", [0, 1, 30])
def test_response_accepts_single_page_lengths(
    response_payload: dict[str, object], file_payload: dict[str, object], count: int
) -> None:
    response_payload["files"] = [dict(file_payload, filename=f"文件 {i}.txt") for i in range(count)]
    result = GitHubPullRequestFilesResponse.model_validate(response_payload)
    assert len(result.files) == count
    assert type(result.per_page) is int
    assert result.per_page == 30
    assert [file.filename for file in result.files] == [f"文件 {i}.txt" for i in range(count)]


@pytest.mark.parametrize("files", [None, {}, "files", (), [None], [{}]])
def test_response_rejects_invalid_files(response_payload: dict[str, object], files: object) -> None:
    response_payload["files"] = files
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"][0] == "files"


def test_response_rejects_more_than_one_page(
    response_payload: dict[str, object], file_payload: dict[str, object]
) -> None:
    response_payload["files"] = [file_payload.copy() for _ in range(31)]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFilesResponse.model_validate(response_payload)
    assert error.value.errors()[0]["loc"] == ("files",)


@pytest.mark.parametrize("field", ["filename", "previous_filename"])
@pytest.mark.parametrize(
    "filename", ["文档/使用说明.md", "source file.py", "README.md", "image.png", "UPPER.PY", " a "]
)
def test_file_names_are_preserved_metadata(
    file_payload: dict[str, object], field: str, filename: str
) -> None:
    file_payload[field] = filename
    assert GitHubPullRequestFile.model_validate(file_payload).model_dump()[field] == filename


@pytest.mark.parametrize("field", ["filename", "previous_filename"])
@pytest.mark.parametrize("value", ["", 42, True, b"file.py", []])
def test_file_names_require_nonempty_strings(
    file_payload: dict[str, object], field: str, value: object
) -> None:
    file_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == (field,)


def test_only_previous_filename_can_be_null(file_payload: dict[str, object]) -> None:
    file_payload["previous_filename"] = None
    assert GitHubPullRequestFile.model_validate(file_payload).previous_filename is None
    file_payload["filename"] = None
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == ("filename",)


@pytest.mark.parametrize(
    "status", ["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
)
def test_file_accepts_documented_statuses(file_payload: dict[str, object], status: str) -> None:
    file_payload["status"] = status
    assert GitHubPullRequestFile.model_validate(file_payload).status == status


@pytest.mark.parametrize("status", ["", "Modified", "deleted", None, 1, True])
def test_file_rejects_unknown_statuses(file_payload: dict[str, object], status: object) -> None:
    file_payload["status"] = status
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == ("status",)


@pytest.mark.parametrize("field", ["additions", "deletions", "changes"])
@pytest.mark.parametrize("value", [0, 1, 2**63])
def test_file_counts_accept_nonnegative_integers(
    file_payload: dict[str, object], field: str, value: int
) -> None:
    file_payload[field] = value
    result = GitHubPullRequestFile.model_validate(file_payload).model_dump()[field]
    assert result == value
    assert type(result) is int


@pytest.mark.parametrize("field", ["additions", "deletions", "changes"])
@pytest.mark.parametrize("value", [-1, True, False, 0.0, "0", None])
def test_file_counts_reject_negative_values_and_coercion(
    file_payload: dict[str, object], field: str, value: object
) -> None:
    file_payload[field] = value
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", sorted(FILE_FIELDS - {"previous_filename"}))
def test_file_requires_five_fields(file_payload: dict[str, object], field: str) -> None:
    del file_payload[field]
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("field", ["patch", "sha", "raw_url", "contents_url"])
def test_file_forbids_upstream_only_fields(file_payload: dict[str, object], field: str) -> None:
    file_payload[field] = "not part of this response"
    with pytest.raises(ValidationError) as error:
        GitHubPullRequestFile.model_validate(file_payload)
    assert error.value.errors()[0]["loc"] == (field,)


def test_nested_serialization_preserves_exact_contract(response_payload: dict[str, object]) -> None:
    result = GitHubPullRequestFilesResponse.model_validate(response_payload)
    data = json.loads(result.model_dump_json())
    assert set(data) == RESPONSE_FIELDS
    assert set(data["files"][0]) == FILE_FIELDS
    assert data["files"][0]["previous_filename"] is None
    assert data["files"][0]["filename"] == "文档/使用说明.md"
    assert GitHubPullRequestFilesResponse.model_validate_json(result.model_dump_json()) == result


def test_does_not_infer_relationships_or_rename_history(file_payload: dict[str, object]) -> None:
    file_payload.update(status="renamed", additions=1, deletions=2, changes=0)
    result = GitHubPullRequestFile.model_validate(file_payload)
    assert (result.additions, result.deletions, result.changes) == (1, 2, 0)
    assert result.previous_filename is None
