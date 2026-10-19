"""使用 AST 静态分析源码，不执行提交的代码。"""

import ast

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

    issues: list[ReviewIssue] = []
    for node in ast.walk(tree):
        # 只检查 eval(...) 和 print(...)，不推断别名或对象方法的实际含义。
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "eval":
                issues.append(
                    ReviewIssue(
                        rule_id="PY_EVAL",
                        severity="error",
                        line=node.lineno,
                        message="避免使用 eval()；它可能执行不可信的代码。",
                    )
                )
            elif node.func.id == "print":
                issues.append(
                    ReviewIssue(
                        rule_id="PY_PRINT",
                        severity="info",
                        line=node.lineno,
                        message="发现 print() 调用；请确认是否需要保留调试输出。",
                    )
                )
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            # except: 没有限定异常类型，可能掩盖程序错误或拦截退出信号。
            issues.append(
                ReviewIssue(
                    rule_id="PY_BARE_EXCEPT",
                    severity="warning",
                    line=node.lineno,
                    message="避免使用裸 except:；请指定需要捕获的异常类型。",
                )
            )

    # ast.walk 的遍历顺序不保证与源码行号一致，所以返回前统一排序。
    return sorted(issues, key=lambda issue: issue.line)
