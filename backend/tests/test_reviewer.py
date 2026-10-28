"""直接验证静态审查器的规则、顺序、语法错误和不执行源码的约定。"""

from typing import NoReturn

import pytest

from app.reviewer import review_python


def test_print_call_returns_py_print():
    """print 调用应返回 PY_PRINT，并定位到调用所在行。"""
    issues = review_python("value = 1\nprint(value)\n")

    assert len(issues) == 1
    assert issues[0].rule_id == "PY_PRINT"
    assert issues[0].severity == "info"
    assert issues[0].line == 2
    assert issues[0].message


def test_multiple_issues_on_same_line_have_stable_order():
    """同一行的问题保留现有遍历顺序，多次审查返回相同结果。"""
    code = "print(eval('1 + 1')); print('again')"
    issues = review_python(code)

    # ast.walk 按层遍历，两个外层 print 调用先于内层 eval 被访问。
    assert [(issue.rule_id, issue.line) for issue in issues] == [
        ("PY_PRINT", 1),
        ("PY_PRINT", 1),
        ("PY_EVAL", 1),
    ]
    assert review_python(code) == issues


def test_submitted_code_does_not_write_file(tmp_path):
    """源码中的写文件语句只能被解析，不能实际创建文件。"""
    target = tmp_path / "reviewer-must-not-create-this.txt"
    # !r 将路径转换成带引号和转义的 Python 字符串字面量，兼容 Windows 路径。
    code = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('unexpected execution', encoding='utf-8')\n"
    )
    assert not target.exists()

    issues = review_python(code)

    assert issues == []
    assert not target.exists()


def test_syntax_error_returns_py_syntax_with_correct_line():
    """第 3 行的语法错误应返回一条 PY_SYNTAX，保留正确行号。"""
    issues = review_python("value = 1\n\nif :\n    pass\n")

    assert len(issues) == 1
    assert issues[0].rule_id == "PY_SYNTAX"
    assert issues[0].severity == "error"
    assert issues[0].line == 3
    assert issues[0].message.startswith("Python 语法错误：")


@pytest.mark.parametrize(
    ("parser_error", "expected_message"),
    [
        pytest.param(
            ValueError("模拟无效源码"),
            "Python 源码无法解析：模拟无效源码",
            id="value-error",
        ),
        pytest.param(
            RecursionError("模拟解析层级过深"),
            "Python 源码无法解析：模拟解析层级过深",
            id="recursion-error",
        ),
        pytest.param(
            SyntaxError("模拟缺少行号"),
            "Python 语法错误：模拟缺少行号",
            id="syntax-without-line",
        ),
    ],
)
def test_parser_errors_return_syntax_issue_with_fallback_line(
    monkeypatch: pytest.MonkeyPatch,
    parser_error: SyntaxError | ValueError | RecursionError,
    expected_message: str,
) -> None:
    """解析器失败且没有行号时，应返回结构正确、可定位到第 1 行的问题。"""

    def fail_to_parse(_code: str) -> NoReturn:
        raise parser_error

    # 临时模拟解析失败，不使用可能耗尽内存或调用栈的输入来触发异常。
    # 离开 with 后恢复解析器，避免影响 pytest 自己的错误分析。
    with monkeypatch.context() as patch:
        patch.setattr("app.reviewer.ast.parse", fail_to_parse)
        issues = review_python("print('before parsing fails')")

    assert len(issues) == 1
    assert issues[0].rule_id == "PY_SYNTAX"
    assert issues[0].severity == "error"
    assert issues[0].line == 1
    assert issues[0].message == expected_message


def test_syntax_error_does_not_return_partial_rule_results() -> None:
    """源码后半段有语法错误时，只报告解析失败，不返回前面 print 的问题。"""
    issues = review_python("print('before error')\nif :\n    pass\n")

    assert len(issues) == 1
    assert issues[0].rule_id == "PY_SYNTAX"
    assert issues[0].severity == "error"
    assert issues[0].line == 2
    assert issues[0].message.startswith("Python 语法错误：")
