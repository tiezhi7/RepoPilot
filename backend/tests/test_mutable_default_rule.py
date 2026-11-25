"""验证可变默认参数规则的边界、定位及不执行源码的约定。"""

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import rules
from app.reviewer import review_python
from app.schemas import ReviewIssue, ReviewResponse

MESSAGE = "可变默认参数可能在多次调用间共享状态；建议使用 None，并在函数内创建新对象。"


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("def f(items={default}):\n    pass\n", id="function"),
        pytest.param("async def f(items={default}):\n    pass\n", id="async-function"),
        pytest.param("f = lambda items={default}: None\n", id="lambda"),
    ],
)
@pytest.mark.parametrize(
    "default",
    [pytest.param("[]", id="list"), pytest.param("{}", id="dict"), pytest.param("{1}", id="set")],
)
def test_supported_function_kinds_and_literals(template: str, default: str) -> None:
    issues = review_python(template.format(default=default))

    assert [issue.model_dump() for issue in issues] == [
        {
            "rule_id": "PY_MUTABLE_DEFAULT",
            "severity": "warning",
            "line": 1,
            "message": MESSAGE,
        }
    ]


@pytest.mark.parametrize(
    "parameters",
    ["items=[], /", "first, items=[]", "*, items=[]", "*args, items=[]"],
)
def test_parameter_positions(parameters: str) -> None:
    issues = review_python(f"def f({parameters}):\n    pass\n")

    assert [(issue.rule_id, issue.line) for issue in issues] == [("PY_MUTABLE_DEFAULT", 1)]


def test_each_default_is_reported_in_order_and_required_keywords_are_skipped() -> None:
    code = (
        "def f(\n"
        "    first=[],\n"
        "    second={},\n"
        "    /,\n"
        "    third={1},\n"
        "    *args,\n"
        "    required,\n"
        "    fourth=[],\n"
        "    fifth={},\n"
        "    **kwargs,\n"
        "):\n"
        "    pass\n"
    )

    issues = review_python(code)

    assert [(issue.rule_id, issue.line) for issue in issues] == [
        ("PY_MUTABLE_DEFAULT", 2),
        ("PY_MUTABLE_DEFAULT", 3),
        ("PY_MUTABLE_DEFAULT", 5),
        ("PY_MUTABLE_DEFAULT", 8),
        ("PY_MUTABLE_DEFAULT", 9),
    ]
    assert all(issue.severity == "warning" and issue.message == MESSAGE for issue in issues)


def test_multiline_default_uses_literal_line_instead_of_function_line() -> None:
    code = "def f(\n    items=(\n        [\n            1,\n        ]\n    ),\n):\n    pass\n"

    issues = review_python(code)

    assert [(issue.rule_id, issue.line) for issue in issues] == [("PY_MUTABLE_DEFAULT", 3)]


@pytest.mark.parametrize(
    "default",
    [
        "None",
        "True",
        "0",
        "'text'",
        "b'bytes'",
        "()",
        "([],)",
        "DEFAULT",
        "list()",
        "dict()",
        "set()",
        "factory()",
        "[item for item in ()]",
        "{item: item for item in ()}",
        "{item for item in ()}",
        "(item for item in ())",
    ],
)
def test_excluded_defaults_are_not_inferred_or_searched_recursively(default: str) -> None:
    """调用、推导式及元组内部对象均不属于第一批的直接字面量匹配。"""
    assert review_python(f"def f(items={default}):\n    pass\n") == []


@pytest.mark.parametrize(
    "code",
    [
        "items = []\noptions = {}\nunique = {1}\n",
        "def f(items=None):\n    local = []\n    return {}\n",
        "def f(items: [1] = None) -> {}:\n    return []\n",
    ],
)
def test_literals_outside_defaults_are_not_reported(code: str) -> None:
    assert review_python(code) == []


def test_helper_only_checks_current_function_node() -> None:
    code = "def outer():\n    def inner(items=[]):\n        pass\n"
    tree = ast.parse(code)
    outer = tree.body[0]
    assert isinstance(outer, ast.FunctionDef)

    assert rules.check_mutable_defaults(tree) == []
    assert rules.check_mutable_defaults(outer) == []
    assert [
        (issue.rule_id, issue.line) for issue in rules.check_mutable_defaults(outer.body[0])
    ] == [("PY_MUTABLE_DEFAULT", 2)]
    assert [(issue.rule_id, issue.line) for issue in review_python(code)] == [
        ("PY_MUTABLE_DEFAULT", 2)
    ]


def test_new_and_existing_rules_are_sorted_without_losing_nested_issues() -> None:
    code = (
        "def outer(items=[]):\n"
        "    def inner(options={}):\n"
        "        print(eval('1 + 1'))\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "print('end')\n"
    )

    issues = review_python(code)

    assert [(issue.rule_id, issue.severity, issue.line) for issue in issues] == [
        ("PY_MUTABLE_DEFAULT", "warning", 1),
        ("PY_MUTABLE_DEFAULT", "warning", 2),
        ("PY_PRINT", "info", 3),
        ("PY_EVAL", "error", 3),
        ("PY_BARE_EXCEPT", "warning", 6),
        ("PY_PRINT", "info", 8),
    ]
    assert review_python(code) == issues


def test_syntax_error_still_suppresses_partial_rule_results() -> None:
    issues = review_python("def f(items=[]):\n    pass\nif :\n    pass\n")

    assert [(issue.rule_id, issue.severity, issue.line) for issue in issues] == [
        ("PY_SYNTAX", "error", 3)
    ]


def test_default_expression_is_not_executed(tmp_path: Path) -> None:
    target = tmp_path / "default-must-not-run.txt"
    code = (
        "from pathlib import Path\n"
        f"def f(items=[Path({str(target)!r}).write_text('unexpected', encoding='utf-8')]):\n"
        "    raise RuntimeError('function body must not run')\n"
        "raise RuntimeError('module must not run')\n"
    )
    assert not target.exists()

    issues = review_python(code)

    assert [(issue.rule_id, issue.line) for issue in issues] == [("PY_MUTABLE_DEFAULT", 2)]
    assert not target.exists()


def test_new_issue_serializes_inside_existing_review_response() -> None:
    issue = ReviewIssue(rule_id="PY_MUTABLE_DEFAULT", severity="warning", line=2, message=MESSAGE)
    response = ReviewResponse(language="python", issue_count=1, issues=[issue])

    assert json.loads(response.model_dump_json()) == {
        "language": "python",
        "issue_count": 1,
        "issues": [
            {
                "rule_id": "PY_MUTABLE_DEFAULT",
                "severity": "warning",
                "line": 2,
                "message": MESSAGE,
            }
        ],
    }


@pytest.mark.parametrize(
    ("rule_id", "severity"),
    [
        ("PY_EVAL", "error"),
        ("PY_PRINT", "info"),
        ("PY_BARE_EXCEPT", "warning"),
        ("PY_SYNTAX", "error"),
    ],
)
def test_original_rule_ids_remain_valid(rule_id: str, severity: str) -> None:
    data = {"rule_id": rule_id, "severity": severity, "line": 1, "message": "existing issue"}

    assert ReviewIssue.model_validate(data).model_dump() == data


@pytest.mark.parametrize("rule_id", ["PY_UNKNOWN", "py_mutable_default", "PY_MUTABLE_DEFAULT\n"])
def test_rule_id_validation_remains_strict(rule_id: str) -> None:
    with pytest.raises(ValidationError) as error:
        ReviewIssue.model_validate(
            {"rule_id": rule_id, "severity": "warning", "line": 1, "message": MESSAGE}
        )

    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("rule_id",), "literal_error")
    ]
