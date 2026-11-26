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


def check_broad_except_pass(node: ast.AST) -> ReviewIssue | None:
    """识别直接捕获 Exception/BaseException 后仅执行 pass 的处理分支。"""
    # 只按直接名称和单个 pass 匹配；不推断别名、元组或名称绑定。
    if (
        not isinstance(node, ast.ExceptHandler)
        or not isinstance(node.type, ast.Name)
        or node.type.id not in ("Exception", "BaseException")
        or len(node.body) != 1
        or not isinstance(node.body[0], ast.Pass)
    ):
        return None
    return ReviewIssue(
        rule_id="PY_BROAD_EXCEPT_PASS",
        severity="warning",
        line=node.lineno,
        message="广泛捕获异常后直接忽略，可能掩盖错误；请确认是否需要处理或重新抛出异常。",
    )


def check_mutable_defaults(node: ast.AST) -> list[ReviewIssue]:
    """检查当前函数的直接可变默认字面量，每个匹配项各返回一条问题。"""
    issues: list[ReviewIssue] = []
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return issues

    # 只检查默认表达式本身；必填关键字参数对应 None，不递归推断变量或调用。
    for default in (*node.args.defaults, *node.args.kw_defaults):
        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
            issues.append(
                ReviewIssue(
                    rule_id="PY_MUTABLE_DEFAULT",
                    severity="warning",
                    line=default.lineno,
                    message="可变默认参数可能在多次调用间共享状态；建议使用 None，并在函数内创建新对象。",
                )
            )
    return issues


def collect_issues(tree: ast.AST) -> list[ReviewIssue]:
    """只遍历一次 AST，对每个节点依次调用规则，收集匹配的问题。"""
    checks = (check_eval, check_print, check_bare_except, check_broad_except_pass)
    issues: list[ReviewIssue] = []

    # 遍历顺序沿用 ast.walk；每条规则只检查当前节点，不再遍历整棵树。
    for node in ast.walk(tree):
        for check in checks:
            issue = check(node)
            if issue is not None:
                issues.append(issue)
        issues.extend(check_mutable_defaults(node))

    return issues
