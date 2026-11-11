"""用本机标准库服务验证真实冒烟脚本子进程，不导入应用或请求 GitHub。"""

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_http.py"
PR_PATH = "/api/v1/github/pull-request"
PR_REQUEST = {"repository_url": "https://github.com/example/repo", "pull_number": 0}
VALID_DETAIL = [{"loc": ["body", "pull_number"], "type": "greater_than_equal", "msg": "编号过小"}]
SUCCESS_MESSAGE = "PASS 全部十项真实 HTTP 检查"


@dataclass
class Scenario:
    pr_method: str | None = "post"
    pr_status: int = 422
    pr_detail: object = field(default_factory=lambda: VALID_DETAIL)
    extra_route: bool = False
    requests: list[tuple[str, str, object]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def response_for(scenario: Scenario, method: str, path: str, payload: object) -> tuple[int, object]:
    """响应数据独立于生产脚本；源码只用作字符串匹配，不执行或审查。"""
    if method == "GET" and path == "/health":
        return 200, {"status": "ok"}
    if method == "GET" and path == "/docs":
        return 200, b'<!doctype html><div class="swagger-ui">SwaggerUIBundle</div>'
    if method == "GET" and path == "/openapi.json":
        paths = {
            "/health": {"get": {}},
            "/api/v1/reviews": {"post": {}},
            "/api/v1/github/repository": {"post": {}},
            "/api/v1/github/file-review": {"post": {}},
        }
        if scenario.pr_method is not None:
            paths[PR_PATH] = {scenario.pr_method: {}}
        if scenario.extra_route:
            paths["/future-route"] = {"get": {}}
        return 200, {"paths": paths}
    if method == "POST" and path == PR_PATH:
        return scenario.pr_status, {"detail": scenario.pr_detail}
    if method == "POST" and path == "/api/v1/reviews":
        if payload == {"code": ""}:
            return 422, {"detail": [{"loc": ["body", "code"], "type": "string_too_short"}]}
        if payload in (
            {"code": "x = 1\n"},
            {"code": 'raise RuntimeError("DAY9_MUST_NOT_EXECUTE")\n'},
        ):
            return 200, {"language": "python", "issue_count": 0, "issues": []}
        if payload == {"code": "print('day9')\n"}:
            return 200, {
                "language": "python",
                "issue_count": 1,
                "issues": [{"rule_id": "PY_PRINT", "severity": "info", "line": 1}],
            }
        if payload == {"code": "def broken(:\n"}:
            return 200, {
                "language": "python",
                "issue_count": 1,
                "issues": [{"rule_id": "PY_SYNTAX", "severity": "error", "line": 1}],
            }
    scenario.errors.append(f"非预期请求：{method} {path} {payload!r}")
    return 500, {"detail": "测试服务收到非预期请求"}


@contextmanager
def local_server(scenario: Scenario) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        # 与 Uvicorn 一样使用 HTTP/1.1；脚本负责关闭每次连接。
        protocol_version = "HTTP/1.1"
        timeout = 2.0

        def handle_request(self) -> None:
            payload = None
            if self.command == "POST":
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(body)
            scenario.requests.append((self.command, self.path, payload))
            status, result = response_for(scenario, self.command, self.path, payload)
            data = result if isinstance(result, bytes) else json.dumps(result).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/html" if isinstance(result, bytes) else "application/json"
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self.handle_request()

        def do_POST(self) -> None:
            self.handle_request()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    class Server(HTTPServer):
        # 不共享地址，收尾时才能通过重新绑定确认原端口已经释放。
        allow_reuse_address = False

    server = Server(("127.0.0.1", 0), Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield port
    finally:
        # subprocess.run 超时会终止并等待自己创建的子进程；此处仍关闭服务。
        try:
            server.shutdown()
        finally:
            server.server_close()
            thread.join(timeout=5)
        assert not thread.is_alive(), "临时 HTTP 服务线程未退出"
        # 实际重新绑定原端口；失败必须保留，不能将连接超时视为释放证据。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        assert scenario.errors == []


def run_smoke(
    port: int, tmp_path: Path, *, optimized: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-B"]
    if optimized:
        command.append("-O")
    command.extend([str(SCRIPT), "--port", str(port)])
    environment = os.environ.copy()
    environment.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    (tmp_path / "command.json").write_text(json.dumps(command), encoding="utf-8")
    stdout_path = tmp_path / "stdout.txt"
    stderr_path = tmp_path / "stderr.txt"
    # 直接写入临时日志，即使子进程超时也保留当时的诊断输出。
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stderr_path.open("w", encoding="utf-8") as stderr,
    ):
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            timeout=20,
            check=False,
        )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout_path.read_text(encoding="utf-8"),
        stderr_path.read_text(encoding="utf-8"),
    )


@pytest.mark.parametrize("extra_route", [False, True], ids=["five-routes", "allows-future-route"])
def test_ten_checks_send_expected_requests(tmp_path: Path, extra_route: bool) -> None:
    scenario = Scenario(extra_route=extra_route)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert "PASS OpenAPI 五个业务路由" in result.stdout
    assert SUCCESS_MESSAGE in result.stdout
    for name in (
        "初始健康检查",
        "Swagger HTML（未加载 CDN）",
        "OpenAPI 五个业务路由",
        "正常源码",
        "print 调用",
        "非法语法",
        "空 code 验证",
        "源码不执行",
        "PR 编号验证",
        "最后健康检查",
    ):
        assert f"PASS {name}" in result.stdout
    assert scenario.requests == [
        ("GET", "/health", None),
        ("GET", "/docs", None),
        ("GET", "/openapi.json", None),
        ("POST", "/api/v1/reviews", {"code": "x = 1\n"}),
        ("POST", "/api/v1/reviews", {"code": "print('day9')\n"}),
        ("POST", "/api/v1/reviews", {"code": "def broken(:\n"}),
        ("POST", "/api/v1/reviews", {"code": ""}),
        ("POST", "/api/v1/reviews", {"code": 'raise RuntimeError("DAY9_MUST_NOT_EXECUTE")\n'}),
        ("POST", PR_PATH, PR_REQUEST),
        ("GET", "/health", None),
    ]
    pr_requests = [payload for method, path, payload in scenario.requests if path == PR_PATH]
    assert len(pr_requests) == 1
    assert type(pr_requests[0]["pull_number"]) is int


@pytest.mark.parametrize("method", [None, "get"], ids=["missing-pr-route", "wrong-pr-method"])
def test_openapi_requires_pr_post(tmp_path: Path, method: str | None) -> None:
    scenario = Scenario(pr_method=method)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert "缺少路由或方法：/api/v1/github/pull-request" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert [path for _, path, _ in scenario.requests] == ["/health", "/docs", "/openapi.json"]


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(404, VALID_DETAIL, "无效 PR 编号应返回 422", id="not-found"),
        pytest.param(500, VALID_DETAIL, "无效 PR 编号应返回 422", id="server-error"),
        pytest.param(422, "错误说明", "detail 应为非空验证错误数组", id="string-detail"),
        pytest.param(422, [], "detail 应为非空验证错误数组", id="empty-detail"),
        pytest.param(
            422, [{"loc": ["body", "repository_url"]}], "验证错误未指向", id="wrong-field"
        ),
        pytest.param(422, [{"loc": ["pull_number"]}], "验证错误未指向", id="missing-body"),
        pytest.param(
            422, [{"loc": ["body", "pull_number", "extra"]}], "验证错误未指向", id="nested-loc"
        ),
        pytest.param(422, ["pull_number"], "验证错误未指向", id="non-object-error"),
    ],
)
def test_bad_pr_validation_fails(tmp_path: Path, status: int, detail: object, message: str) -> None:
    scenario = Scenario(pr_status=status, pr_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"PR 编号验证: HTTP {status}" in result.stdout
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", PR_PATH, PR_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


def test_optimized_python_still_rejects_wrong_pr_location(tmp_path: Path) -> None:
    scenario = Scenario(pr_detail=[{"loc": ["body", "repository_url"]}])
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 1
    assert "验证错误未指向 body.pull_number 字段" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", PR_PATH, PR_REQUEST)
