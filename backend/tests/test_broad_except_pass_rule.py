"""验证广泛捕获后仅 pass 的匹配边界、定位及源码不执行。"""

import ast
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import rules
from app.reviewer import review_python
from app.schemas import ReviewIssue, ReviewResponse

MESSAGE = "广泛捕获异常后直接忽略，可能掩盖错误；请确认是否需要处理或重新抛出异常。"


@pytest.mark.parametrize("exception", ["Exception", "BaseException"])
@pytest.mark.parametrize("keyword", ["except", "except*"])
@pytest.mark.parametrize("binding", ["", " as error"])
def test_broad_handler_with_only_pass_has_exact_issue(
    exception: str, keyword: str, binding: str
) -> None:
    code = f"try:\n    work()\n{keyword} {exception}{binding}:\n    pass\n"

    assert [issue.model_dump() for issue in review_python(code)] == [
        {
            "rule_id": "PY_BROAD_EXCEPT_PASS",
            "severity": "warning",
            "line": 3,
            "message": MESSAGE,
        }
    ]


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("try: work()\nexcept Exception: pass\n", 2),
        ("try:\n    work()\nexcept (\n    Exception\n):\n    pass\n", 3),
        ("try:\n    work()\nexcept Exception:\n    # intentionally ignored\n    pass\n", 3),
        ("Exception = ValueError\ntry:\n    work()\nexcept Exception:\n    pass\n", 4),
    ],
)
def test_location_and_syntactic_name_matching(code: str, line: int) -> None:
    # 注释不改变 AST；本批按直接名称匹配，不推断同名变量绑定。
    assert [(issue.rule_id, issue.line) for issue in review_python(code)] == [
        ("PY_BROAD_EXCEPT_PASS", line)
    ]


@pytest.mark.parametrize(
    "exception",
    [
        "ValueError",
        "ExceptionGroup",
        "(Exception, ValueError)",
        "(BaseException,)",
        "builtins.Exception",
        "builtins.BaseException",
        "Alias",
        "exception",
    ],
)
def test_specific_types_tuples_attributes_and_aliases_are_excluded(exception: str) -> None:
    assert review_python(f"try:\n    work()\nexcept {exception}:\n    pass\n") == []


@pytest.mark.parametrize(
    "body",
    [
        "        raise\n",
        "        logger.warning('failed')\n",
        "        logger.warning('failed')\n        pass\n",
        "        pass\n        pass\n",
        "        ...\n",
        "        'ignored'\n        pass\n",
        "        if enabled:\n            pass\n",
        "        return\n",
    ],
)
def test_other_handler_bodies_are_excluded(body: str) -> None:
    code = "def f():\n    try:\n        work()\n    except Exception:\n" + body

    assert review_python(code) == []


def test_bare_except_keeps_only_its_existing_warning() -> None:
    issues = review_python("try:\n    work()\nexcept:\n    pass\n")

    assert [(issue.rule_id, issue.severity, issue.line) for issue in issues] == [
        ("PY_BARE_EXCEPT", "warning", 3)
    ]


def test_helper_only_checks_current_handler_and_nested_warning_is_not_duplicated() -> None:
    code = (
        "try:\n    work()\nexcept Exception:\n"
        "    try:\n        work()\n    except Exception:\n        pass\n"
    )
    tree = ast.parse(code)
    outer_try = tree.body[0]
    assert isinstance(outer_try, ast.Try)
    outer = outer_try.handlers[0]
    inner_try = outer.body[0]
    assert isinstance(inner_try, ast.Try)

    assert rules.check_broad_except_pass(tree) is None
    assert rules.check_broad_except_pass(outer) is None
    issue = rules.check_broad_except_pass(inner_try.handlers[0])
    assert issue is not None
    assert issue.model_dump() == {
        "rule_id": "PY_BROAD_EXCEPT_PASS",
        "severity": "warning",
        "line": 6,
        "message": MESSAGE,
    }
    assert review_python(code) == [issue]


def test_new_and_existing_rules_preserve_count_and_order() -> None:
    code = (
        "def f(items=[]):\n"
        "    print(eval('1'))\n"
        "    try:\n"
        "        work()\n"
        "    except Exception:\n"
        "        pass\n"
        "try:\n"
        "    pass\n"
        "except:\n"
        "    pass\n"
        "print('end')\n"
    )

    assert [(issue.rule_id, issue.severity, issue.line) for issue in review_python(code)] == [
        ("PY_MUTABLE_DEFAULT", "warning", 1),
        ("PY_PRINT", "info", 2),
        ("PY_EVAL", "error", 2),
        ("PY_BROAD_EXCEPT_PASS", "warning", 5),
        ("PY_BARE_EXCEPT", "warning", 9),
        ("PY_PRINT", "info", 11),
    ]


def test_syntax_error_suppresses_partial_rule_results() -> None:
    code = "try:\n    work()\nexcept Exception:\n    pass\nif :\n    pass\n"

    assert [(issue.rule_id, issue.severity, issue.line) for issue in review_python(code)] == [
        ("PY_SYNTAX", "error", 5)
    ]


def test_submitted_code_is_not_executed(tmp_path: Path) -> None:
    target = tmp_path / "broad-except-must-not-run.txt"
    code = (
        "from pathlib import Path\n"
        "try:\n"
        f"    Path({str(target)!r}).write_text('unexpected', encoding='utf-8')\n"
        "except Exception:\n"
        "    pass\n"
        "raise RuntimeError('submitted code must not run')\n"
    )
    assert not target.exists()

    issues = review_python(code)

    assert [(issue.rule_id, issue.line) for issue in issues] == [("PY_BROAD_EXCEPT_PASS", 4)]
    assert not target.exists()


def test_new_issue_serializes_inside_existing_response() -> None:
    issue = ReviewIssue(rule_id="PY_BROAD_EXCEPT_PASS", severity="warning", line=3, message=MESSAGE)
    response = ReviewResponse(language="python", issue_count=1, issues=[issue])

    assert json.loads(response.model_dump_json()) == {
        "language": "python",
        "issue_count": 1,
        "issues": [
            {
                "rule_id": "PY_BROAD_EXCEPT_PASS",
                "severity": "warning",
                "line": 3,
                "message": MESSAGE,
            }
        ],
    }


@pytest.mark.parametrize(
    "rule_id", ["PY_BROAD_EXCEPT", "py_broad_except_pass", "PY_BROAD_EXCEPT_PASS\n"]
)
def test_new_rule_id_remains_a_strict_literal(rule_id: str) -> None:
    with pytest.raises(ValidationError) as error:
        ReviewIssue.model_validate(
            {"rule_id": rule_id, "severity": "warning", "line": 3, "message": MESSAGE}
        )

    assert [(item["loc"], item["type"]) for item in error.value.errors()] == [
        (("rule_id",), "literal_error")
    ]
