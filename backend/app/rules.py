"""独立的 AST 节点规则及统一收集器，不执行提交的源码。"""

import ast

from .schemas import ReviewIssue


def check_eval(node: ast.AST) -> ReviewIssue | None:
    """识别直接调用的 eval(...)；不匹配时返回 None。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "eval":
        return ReviewIssue(
            rule_id="PY_EVAL",
            severity="error",
            line=node.lineno,
            message="避免使用 eval()；它可能执行不可信的代码。",
        )
    return None


def check_print(node: ast.AST) -> ReviewIssue | None:
    """识别直接调用的 print(...)；不推断对象方法或函数别名。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
        return ReviewIssue(
            rule_id="PY_PRINT",
            severity="info",
            line=node.lineno,
            message="发现 print() 调用；请确认是否需要保留调试输出。",
        )
    return None


def check_bare_except(node: ast.AST) -> ReviewIssue | None:
    """识别没有指定异常类型的 except:。"""
    if isinstance(node, ast.ExceptHandler) and node.type is None:
        return ReviewIssue(
            rule_id="PY_BARE_EXCEPT",
            severity="warning",
            line=node.lineno,
            message="避免使用裸 except:；请指定需要捕获的异常类型。",
        )
    return None


def collect_issues(tree: ast.AST) -> list[ReviewIssue]:
    """只遍历一次 AST，对每个节点依次调用规则，收集匹配的问题。"""
    checks = (check_eval, check_print, check_bare_except)
    issues: list[ReviewIssue] = []

    # 遍历顺序沿用 ast.walk；每条规则只检查当前节点，不再遍历整棵树。
    for node in ast.walk(tree):
        for check in checks:
            issue = check(node)
            if issue is not None:
                issues.append(issue)

    return issues
