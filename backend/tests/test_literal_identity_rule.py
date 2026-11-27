"""验证字面量身份比较的匹配边界、链式配对、定位与源码不执行。"""

import ast
import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import ValidationError

from app import rules
from app.reviewer import review_python
from app.schemas import ReviewIssue, ReviewResponse

MESSAGE = "使用 is / is not 比较字面量可能产生非预期结果；请确认是否应使用 == / !=。"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("规则测试禁止真实网络和 DNS 查询")

    monkeypatch.setattr(socket, "getaddrinfo", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket.socket, "connect", fail_network)
    monkeypatch.setattr(socket.socket, "connect_ex", fail_network)


def test_string_identity_has_exact_warning() -> None:
    assert [issue.model_dump() for issue in review_python('value is "ready"')] == [
        {
            "rule_id": "PY_LITERAL_IDENTITY",
            "severity": "warning",
            "line": 1,
            "message": MESSAGE,
        }
    ]


@pytest.mark.parametrize(
    "code",
    [
        'value is not "ready"',
        '"ready" is value',
        'value is ""',
        'value is "中文"',
        'b"" is not value',
        "value is 0",
        "0x10 is value",
        "value is not 1.5",
        "2j is value",
        "value is -1",
        "-1.5 is not value",
        "value is +2j",
        "value is -2j",
        "value is +1.0",
        "(1_000) is not value",
        'value is ("a" "b")',
        "True is 1",
        "0 is None",
    ],
)
def test_literal_on_either_side_and_signed_numbers_are_reported(code: str) -> None:
    assert [(i.rule_id, i.severity, i.line, i.message) for i in review_python(code)] == [
        ("PY_LITERAL_IDENTITY", "warning", 1, MESSAGE)
    ]


@pytest.mark.parametrize(
    "code",
    [
        "value is None",
        "None is not value",
        "value is True",
        "False is value",
        "value is ...",
        "value is Ellipsis",
        "value is NotImplemented",
        "a is b",
        "value == 1",
        'value != "ready"',
        "value < 1",
        "value in (1, 2)",
        "value is [1]",
        "value is {1}",
        'value is {"key": 1}',
        "value is ()",
        "value is (1,)",
        "value is (1, 2)",
        'value is f"ready"',
        'value is f"{name}"',
        "value is (1 + 2)",
        "value is (1 + 2j)",
        'value is str("ready")',
        "value is -number",
        "value is --1",
        "value is -(1 + 2)",
        "value is -True",
        "value is ~1",
        "NUMBER = 1\nvalue is NUMBER",
        'TEXT = "ready"\nvalue is TEXT',
        '# value is "ready"\ntext = "value is 1"\n',
    ],
)
def test_singletons_names_containers_and_computed_values_are_excluded(code: str) -> None:
    assert review_python(code) == []


@pytest.mark.parametrize(
    ("code", "count"),
    [
        ("a is b == 1", 0),
        ("1 == a is b", 0),
        ("a is b < 1", 0),
        ("a is b is None", 0),
        ("a == 1 is b", 1),
        ("a is 1 is b", 1),
        ("a is not 1 is not 2 is b", 1),
        ("a < 1 is not b", 1),
        ("1 is a is None", 1),
        ("(a is 1) is (b is 2)", 2),
        ("a is (b is 1)", 1),
        ('a is 1 and b is not "x"', 2),
    ],
)
def test_chains_use_adjacent_pairs_and_report_once_per_compare(code: str, count: int) -> None:
    assert [(i.rule_id, i.severity, i.line, i.message) for i in review_python(code)] == [
        ("PY_LITERAL_IDENTITY", "warning", 1, MESSAGE)
    ] * count


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ('\n\nvalue is "ready"\n', 3),
        ('result = (\n    value\n    is "ready"\n)\n', 2),
        ('result = (\n    "ready"\n    is not value\n)\n', 2),
        ("def f():\n    return value is not -1\n", 2),
        ("async def f():\n    return value is 2j\n", 2),
        ('check = lambda value: value is "ok"\n', 1),
    ],
)
def test_location_uses_compare_start_including_multiline_and_functions(
    code: str, line: int
) -> None:
    assert [(i.rule_id, i.line) for i in review_python(code)] == [("PY_LITERAL_IDENTITY", line)]


def test_helper_checks_only_current_compare_without_duplicate_nested_results() -> None:
    code = "outer is (inner is 1)"
    tree = ast.parse(code, mode="eval")
    outer = tree.body
    assert isinstance(outer, ast.Compare)
    inner = outer.comparators[0]
    assert isinstance(inner, ast.Compare)

    assert rules.check_literal_identity(tree) is None
    assert rules.check_literal_identity(outer) is None
    issue = rules.check_literal_identity(inner)
    assert issue is not None
    assert issue.model_dump() == {
        "rule_id": "PY_LITERAL_IDENTITY",
        "severity": "warning",
        "line": 1,
        "message": MESSAGE,
    }
    assert review_python(code) == [issue]


def test_collection_still_walks_ast_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original_walk = ast.walk
    walks: list[ast.AST] = []

    def walk(tree: ast.AST) -> Iterator[ast.AST]:
        walks.append(tree)
        return original_walk(tree)

    monkeypatch.setattr(rules.ast, "walk", walk)

    issues = review_python('a is 1\nb is not "ready"\n')

    assert len(walks) == 1
    assert [(i.rule_id, i.line) for i in issues] == [
        ("PY_LITERAL_IDENTITY", 1),
        ("PY_LITERAL_IDENTITY", 2),
    ]


def test_new_and_existing_rules_preserve_count_and_order() -> None:
    code = (
        "def f(items=[]):\n"
        "    print(eval('1'))\n"
        '    if value is "ready":\n'
        "        pass\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n"
        "try:\n"
        "    pass\n"
        "except:\n"
        "    pass\n"
        "result = other is not 1\n"
    )

    assert [(i.rule_id, i.severity, i.line) for i in review_python(code)] == [
        ("PY_MUTABLE_DEFAULT", "warning", 1),
        ("PY_PRINT", "info", 2),
        ("PY_EVAL", "error", 2),
        ("PY_LITERAL_IDENTITY", "warning", 3),
        ("PY_BROAD_EXCEPT_PASS", "warning", 7),
        ("PY_BARE_EXCEPT", "warning", 11),
        ("PY_LITERAL_IDENTITY", "warning", 13),
    ]


def test_syntax_error_suppresses_partial_rule_results() -> None:
    code = 'value is "ready"\nif :\n    pass\n'

    assert [(i.rule_id, i.severity, i.line) for i in review_python(code)] == [
        ("PY_SYNTAX", "error", 2)
    ]


def test_submitted_code_is_not_executed(tmp_path: Path) -> None:
    target = tmp_path / "literal-identity-must-not-run.txt"
    code = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('unexpected', encoding='utf-8')\n"
        "result = value is 1\n"
        "raise RuntimeError('submitted code must not run')\n"
    )
    assert not target.exists()

    issues = review_python(code)

    assert [(i.rule_id, i.line) for i in issues] == [("PY_LITERAL_IDENTITY", 3)]
    assert not target.exists()


def test_new_issue_serializes_inside_existing_response() -> None:
    issue = ReviewIssue(rule_id="PY_LITERAL_IDENTITY", severity="warning", line=1, message=MESSAGE)
    response = ReviewResponse(language="python", issue_count=1, issues=[issue])

    assert json.loads(response.model_dump_json()) == {
        "language": "python",
        "issue_count": 1,
        "issues": [
            {
                "rule_id": "PY_LITERAL_IDENTITY",
                "severity": "warning",
                "line": 1,
                "message": MESSAGE,
            }
        ],
    }


@pytest.mark.parametrize(
    "rule_id", ["PY_LITERAL_IDENT", "py_literal_identity", "PY_LITERAL_IDENTITY\n"]
)
def test_new_rule_id_remains_a_strict_literal(rule_id: str) -> None:
    with pytest.raises(ValidationError) as error:
        ReviewIssue.model_validate(
            {"rule_id": rule_id, "severity": "warning", "line": 1, "message": MESSAGE}
        )

    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("rule_id",), "literal_error")
    ]
