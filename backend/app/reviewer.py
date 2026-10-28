"""使用 AST 静态分析源码，不执行提交的代码。"""

import ast

from .rules import collect_issues
from .schemas import ReviewIssue


def review_python(code: str) -> list[ReviewIssue]:
    """返回按行号排序的问题；解析失败时返回一条 PY_SYNTAX。"""

    try:
        # AST 是源码的结构化表示；这里只解析，不导入或运行源码。
        tree = ast.parse(code)
    except SyntaxError as error:
        return [
            ReviewIssue(
                rule_id="PY_SYNTAX",
                severity="error",
                line=error.lineno or 1,
                message=f"Python 语法错误：{error.msg}",
            )
        ]
    except (ValueError, RecursionError) as error:
        # 非法字符或过于复杂的表达式也可能导致解析失败，避免返回服务器错误。
        # 这类错误没有可靠行号，因此使用第 1 行作为定位起点。
        return [
            ReviewIssue(
                rule_id="PY_SYNTAX",
                severity="error",
                line=1,
                message=f"Python 源码无法解析：{error}",
            )
        ]

    issues = collect_issues(tree)

    # ast.walk 的遍历顺序不保证与源码行号一致，所以返回前统一排序。
    return sorted(issues, key=lambda issue: issue.line)
