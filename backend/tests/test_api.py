"""验证 API、四条规则、输入边界及源码不会被执行。"""

from typing import NoReturn

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reviewer import review_python


@pytest.fixture
def client():
    # TestClient 在进程内调用 API，测试时不需要另开服务器。
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    """健康检查应成功，并明确返回 status=ok。"""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_normal_python_returns_no_issues(client):
    """不触发规则的正常 Python 代码应返回 0 个问题和空列表。"""
    response = client.post("/api/v1/reviews", json={"code": "total = 1 + 2"})

    assert response.status_code == 200
    assert response.json() == {"language": "python", "issue_count": 0, "issues": []}


def test_eval_call_returns_py_eval(client):
    """eval 调用应被识别为 PY_EVAL，而不是在审查时求值。"""
    # 如果错误地执行下面的源码，会出现除零异常；静态审查仍应正常返回。
    response = client.post("/api/v1/reviews", json={"code": "value = eval('1 / 0')"})

    assert response.status_code == 200
    data = response.json()
    assert data["language"] == "python"
    assert data["issue_count"] == len(data["issues"]) == 1
    issue = data["issues"][0]
    assert issue["rule_id"] == "PY_EVAL"
    assert issue["severity"] == "error"
    assert issue["line"] == 1
    assert issue["message"]


def test_print_call_returns_py_print(client):
    """保留已有覆盖：第 2 行的 print 调用应产生一条提示。"""
    response = client.post("/api/v1/reviews", json={"code": "value = 1\nprint(value)"})

    assert response.status_code == 200
    data = response.json()
    assert data["language"] == "python"
    assert data["issue_count"] == len(data["issues"]) == 1
    issue = data["issues"][0]
    assert issue["rule_id"] == "PY_PRINT"
    assert issue["severity"] == "info"
    assert issue["line"] == 2
    assert issue["message"]


def test_bare_except_returns_py_bare_except(client):
    """未指定异常类型的 except: 应产生 PY_BARE_EXCEPT，定位到 except 行。"""
    response = client.post("/api/v1/reviews", json={"code": "try:\n    pass\nexcept:\n    pass"})

    assert response.status_code == 200
    data = response.json()
    assert data["language"] == "python"
    assert data["issue_count"] == len(data["issues"]) == 1
    issue = data["issues"][0]
    assert issue["rule_id"] == "PY_BARE_EXCEPT"
    assert issue["severity"] == "warning"
    assert issue["line"] == 3
    assert issue["message"]


@pytest.mark.parametrize(
    "code",
    [
        "# eval('1 + 1')\ntext = 'print(123)'",
        "try:\n    pass\nexcept ValueError:\n    pass",
        "printer('hello')\nre_evaluate('1 + 1')",
        "logger.print('hello')\nengine.eval('1 + 1')",
        " \n",
    ],
)
def test_code_without_matching_rules(client, code):
    """注释、字符串、指定类型的 except 及其他不匹配的结构应不触发规则。"""
    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 200
    assert response.json() == {"language": "python", "issue_count": 0, "issues": []}


def test_reviewer_sorts_nested_issues_by_line():
    """即使问题位于不同嵌套层级，返回结果也应按源码行号排序。"""
    # 嵌套函数里的 print 比顶层 eval 更早出现，用于验证不能直接依赖遍历顺序。
    code = (
        "def nested():\n"
        "    print('inside')\n"
        "eval('1 + 1')\n"
        "try:\n"
        "    pass\n"
        "except:\n"
        "    pass\n"
        "print('end')\n"
    )

    issues = review_python(code)

    assert [(issue.rule_id, issue.line) for issue in issues] == [
        ("PY_PRINT", 2),
        ("PY_EVAL", 3),
        ("PY_BARE_EXCEPT", 6),
        ("PY_PRINT", 8),
    ]


def test_multiple_issues_on_same_line(client):
    """同一行的多个调用都应保留，问题计数应与列表长度一致。"""
    response = client.post(
        "/api/v1/reviews",
        json={"code": "print(eval('1 + 1')); print('again')"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issue_count"] == len(data["issues"]) == 3
    assert [issue["line"] for issue in data["issues"]] == [1, 1, 1]
    assert sorted(issue["rule_id"] for issue in data["issues"]) == [
        "PY_EVAL",
        "PY_PRINT",
        "PY_PRINT",
    ]


@pytest.mark.parametrize(
    ("code", "line"),
    [
        ("def broken(:\n    pass", 1),
        ("if True:\nprint(1)", 2),
        ("value = 1\nif :\n    pass", 2),
    ],
)
def test_syntax_error_returns_py_syntax(client, code, line):
    """语法或缩进错误应作为 PY_SYNTAX 返回，而不是让 API 崩溃。"""
    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 200
    data = response.json()
    assert data["issue_count"] == len(data["issues"]) == 1
    issue = data["issues"][0]
    assert issue["rule_id"] == "PY_SYNTAX"
    assert issue["severity"] == "error"
    assert issue["line"] == line
    assert issue["message"].startswith("Python 语法错误：")


@pytest.mark.parametrize("code", ["value = 1\x00", "(" * 300 + "1" + ")" * 300])
def test_unparseable_source_does_not_cause_server_error(client, code):
    """空字符和过深括号导致解析失败时，应返回一条 PY_SYNTAX。"""
    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 200
    data = response.json()
    assert data["issue_count"] == len(data["issues"]) == 1
    assert data["issues"][0]["rule_id"] == "PY_SYNTAX"


def test_invalid_unicode_returns_422(client):
    """非法 Unicode 应返回可读取的 422 响应，并定位到 code 字段。"""
    # 使用原始 JSON 发送无法编码为 UTF-8 的孤立代理字符。
    response = client.post(
        "/api/v1/reviews",
        content=r'{"code":"\ud800"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "code"]
    assert "input" not in detail[0]


@pytest.mark.parametrize("code", ["x", "#" + "x" * 19_999])
def test_code_length_boundaries_are_accepted(client, code):
    """长度恰好为 1 和 20000 的合法代码都应被接受。"""
    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 200
    assert response.json() == {"language": "python", "issue_count": 0, "issues": []}


def test_empty_code_returns_422(client):
    """空字符串应被长度校验拒绝，错误位置必须是请求中的 code 字段。"""
    response = client.post("/api/v1/reviews", json={"code": ""})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(
        error["loc"] == ["body", "code"] and error["type"] == "string_too_short" for error in errors
    )


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"code": "x" * 20_001},
        {"code": 123},
        {"code": False},
        {"code": None},
        {"code": []},
        {"code": {}},
        [],
        "print('hello')",
    ],
)
def test_invalid_request_returns_422(client, body):
    """缺字段、类型错误或超过长度上限的请求应返回 422。"""
    response = client.post("/api/v1/reviews", json=body)

    assert response.status_code == 422
    assert response.json()["detail"]


@pytest.mark.parametrize("body", ["", '{"code":'])
def test_missing_or_malformed_json_returns_422(client, body):
    """没有请求体或 JSON 格式不完整时，应拒绝请求并提供错误说明。"""
    response = client.post(
        "/api/v1/reviews",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]


def test_submitted_code_is_not_executed(client, tmp_path, capsys):
    """提交的写文件、print 和 raise 语句只能被分析，不能产生运行效果。"""
    target = tmp_path / "submitted-code-must-not-create-this.txt"
    code = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('unexpected execution')\n"
        "print('submitted-output-must-not-appear')\n"
        "raise RuntimeError('submitted code was executed')\n"
    )

    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 200
    assert response.json()["issue_count"] == 1
    assert response.json()["issues"][0]["rule_id"] == "PY_PRINT"
    assert not target.exists()
    assert "submitted-output-must-not-appear" not in capsys.readouterr().out


def test_code_over_20000_characters_returns_422(client):
    """长度为 20001 的源码应被拒绝，并明确报告 code 字段过长。"""
    # 使用有效的 Python 注释，确保被拒绝的原因是长度，而不是源码语法。
    code = "#" + "x" * 20_000
    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(
        error["loc"] == ["body", "code"] and error["type"] == "string_too_long" for error in errors
    )


@pytest.mark.parametrize(
    ("code", "error_type"),
    [
        pytest.param("", "string_too_short", id="empty-code"),
        pytest.param("#" + "x" * 20_000, "string_too_long", id="oversized-code"),
    ],
)
def test_invalid_code_is_rejected_before_review(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    error_type: str,
) -> None:
    """长度校验失败应直接返回 422，不进入审查器，也不回显原始输入。"""

    def fail_if_reviewed(_code: str) -> NoReturn:
        pytest.fail("无效长度的 code 不应进入审查器")

    monkeypatch.setattr("app.main.review_python", fail_if_reviewed)

    response = client.post("/api/v1/reviews", json={"code": code})

    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(error["loc"] == ["body", "code"] and error["type"] == error_type for error in errors)
    assert all("input" not in error for error in errors)
