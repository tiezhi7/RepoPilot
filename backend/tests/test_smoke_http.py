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
FILES_PATH = "/api/v1/github/pull-request/files"
FILES_REQUEST = {"repository_url": "https://github.com/example/repo", "pull_number": 1, "page": 0}
VALID_PAGE_DETAIL = [{"loc": ["body", "page"], "type": "value_error", "msg": "页码过小"}]
FILE_REVIEW_PATH = "/api/v1/github/pull-request/file-review"
FILE_REVIEW_REQUEST = {
    "repository_url": "https://github.com/example/repo",
    "pull_number": 1,
    "file_path": "../example.py",
}
VALID_FILE_PATH_DETAIL = [
    {"loc": ["body", "file_path"], "type": "value_error", "msg": "文件路径无效"}
]
BATCH_REVIEW_PATH = "/api/v1/github/pull-request/batch-file-review"
BATCH_REVIEW_REQUEST = {
    "repository_url": "https://github.com/example/repo",
    "pull_number": 1,
    "file_paths": [],
}
VALID_BATCH_PATHS_DETAIL = [
    {"loc": ["body", "file_paths"], "type": "value_error", "msg": "文件路径列表为空"}
]
MUTABLE_DEFAULT_CODE = (
    "def add_item(\n    item, items=[],\n):\n    items.append(item)\n    return items\n"
)
MUTABLE_DEFAULT_REQUEST = {"code": MUTABLE_DEFAULT_CODE}
SUCCESS_MESSAGE = "PASS 全部十四项真实 HTTP 检查"


def mutable_default_response() -> dict[str, object]:
    return {
        "language": "python",
        "issue_count": 1,
        "issues": [
            {
                "rule_id": "PY_MUTABLE_DEFAULT",
                "severity": "warning",
                "line": 2,
                "message": "可变默认参数可能在多次调用间共享状态；建议使用 None，并在函数内创建新对象。",
            }
        ],
    }


@dataclass
class Scenario:
    pr_method: str | None = "post"
    pr_status: int = 422
    pr_detail: object = field(default_factory=lambda: VALID_DETAIL)
    files_method: str | None = "post"
    files_status: int = 422
    files_detail: object = field(default_factory=lambda: VALID_PAGE_DETAIL)
    file_review_method: str | None = "post"
    file_review_status: int = 422
    file_review_detail: object = field(default_factory=lambda: VALID_FILE_PATH_DETAIL)
    batch_review_method: str | None = "post"
    batch_review_status: int = 422
    batch_review_detail: object = field(default_factory=lambda: VALID_BATCH_PATHS_DETAIL)
    mutable_default_status: int = 200
    mutable_default_body: object = field(default_factory=mutable_default_response)
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
        if scenario.files_method is not None:
            paths[FILES_PATH] = {scenario.files_method: {}}
        if scenario.file_review_method is not None:
            paths[FILE_REVIEW_PATH] = {scenario.file_review_method: {}}
        if scenario.batch_review_method is not None:
            paths[BATCH_REVIEW_PATH] = {scenario.batch_review_method: {}}
        if scenario.extra_route:
            paths["/future-route"] = {"get": {}}
        return 200, {"paths": paths}
    if method == "POST" and path == PR_PATH:
        return scenario.pr_status, {"detail": scenario.pr_detail}
    if method == "POST" and path == FILES_PATH:
        return scenario.files_status, {"detail": scenario.files_detail}
    if method == "POST" and path == FILE_REVIEW_PATH:
        return scenario.file_review_status, {"detail": scenario.file_review_detail}
    if method == "POST" and path == BATCH_REVIEW_PATH:
        return scenario.batch_review_status, {"detail": scenario.batch_review_detail}
    if method == "POST" and path == "/api/v1/reviews":
        if payload == MUTABLE_DEFAULT_REQUEST:
            return scenario.mutable_default_status, scenario.mutable_default_body
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


@pytest.mark.parametrize("extra_route", [False, True], ids=["eight-routes", "allows-future-route"])
def test_fourteen_checks_send_expected_requests(tmp_path: Path, extra_route: bool) -> None:
    scenario = Scenario(extra_route=extra_route)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert "PASS OpenAPI 八个业务路由" in result.stdout
    assert SUCCESS_MESSAGE in result.stdout
    for name in (
        "初始健康检查",
        "Swagger HTML（未加载 CDN）",
        "OpenAPI 八个业务路由",
        "正常源码",
        "print 调用",
        "可变默认参数",
        "非法语法",
        "空 code 验证",
        "源码不执行",
        "PR 编号验证",
        "PR 文件页码验证",
        "PR 文件路径验证",
        "PR 批量文件路径验证",
        "最后健康检查",
    ):
        assert f"PASS {name}" in result.stdout
    assert scenario.requests == [
        ("GET", "/health", None),
        ("GET", "/docs", None),
        ("GET", "/openapi.json", None),
        ("POST", "/api/v1/reviews", {"code": "x = 1\n"}),
        ("POST", "/api/v1/reviews", {"code": "print('day9')\n"}),
        ("POST", "/api/v1/reviews", MUTABLE_DEFAULT_REQUEST),
        ("POST", "/api/v1/reviews", {"code": "def broken(:\n"}),
        ("POST", "/api/v1/reviews", {"code": ""}),
        ("POST", "/api/v1/reviews", {"code": 'raise RuntimeError("DAY9_MUST_NOT_EXECUTE")\n'}),
        ("POST", PR_PATH, PR_REQUEST),
        ("POST", FILES_PATH, FILES_REQUEST),
        ("POST", FILE_REVIEW_PATH, FILE_REVIEW_REQUEST),
        ("POST", BATCH_REVIEW_PATH, BATCH_REVIEW_REQUEST),
        ("GET", "/health", None),
    ]
    pr_requests = [payload for method, path, payload in scenario.requests if path == PR_PATH]
    assert len(pr_requests) == 1
    assert type(pr_requests[0]["pull_number"]) is int
    files_requests = [payload for method, path, payload in scenario.requests if path == FILES_PATH]
    assert len(files_requests) == 1
    assert type(files_requests[0]["pull_number"]) is int
    assert type(files_requests[0]["page"]) is int
    file_review_requests = [
        payload for method, path, payload in scenario.requests if path == FILE_REVIEW_PATH
    ]
    assert len(file_review_requests) == 1
    assert type(file_review_requests[0]["pull_number"]) is int
    assert file_review_requests[0]["file_path"] == "../example.py"

    batch_requests = [
        payload for method, path, payload in scenario.requests if path == BATCH_REVIEW_PATH
    ]
    assert len(batch_requests) == 1
    assert type(batch_requests[0]["pull_number"]) is int
    assert type(batch_requests[0]["file_paths"]) is list
    assert batch_requests[0]["file_paths"] == []


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


@pytest.mark.parametrize("method", [None, "get"], ids=["missing-files-route", "wrong-files-method"])
def test_openapi_requires_files_post(tmp_path: Path, method: str | None) -> None:
    scenario = Scenario(files_method=method)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"缺少路由或方法：{FILES_PATH}" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert [path for _, path, _ in scenario.requests] == ["/health", "/docs", "/openapi.json"]


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(200, VALID_PAGE_DETAIL, "无效 PR 文件页码应返回 422", id="unexpected-success"),
        pytest.param(404, VALID_PAGE_DETAIL, "无效 PR 文件页码应返回 422", id="not-found"),
        pytest.param(500, VALID_PAGE_DETAIL, "无效 PR 文件页码应返回 422", id="server-error"),
        pytest.param(503, VALID_PAGE_DETAIL, "无效 PR 文件页码应返回 422", id="rate-limit"),
        pytest.param(422, "错误说明", "detail 应为非空验证错误数组", id="string-detail"),
        pytest.param(422, [], "detail 应为非空验证错误数组", id="empty-detail"),
        pytest.param(422, [{"loc": ["body", "pull_number"]}], "验证错误未指向", id="wrong-field"),
        pytest.param(422, [{"loc": ["page"]}], "验证错误未指向", id="missing-body"),
        pytest.param(422, [{"loc": ["body", "page", "extra"]}], "验证错误未指向", id="nested-loc"),
        pytest.param(422, ["page"], "验证错误未指向", id="non-object-error"),
    ],
)
def test_bad_files_page_validation_fails(
    tmp_path: Path, status: int, detail: object, message: str
) -> None:
    scenario = Scenario(files_status=status, files_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"PR 文件页码验证: HTTP {status}" in result.stdout
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", FILES_PATH, FILES_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


def test_optimized_python_still_rejects_wrong_page_location(tmp_path: Path) -> None:
    scenario = Scenario(files_detail=[{"loc": ["body", "pull_number"]}])
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 1
    assert "验证错误未指向 body.page 字段" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", FILES_PATH, FILES_REQUEST)


@pytest.mark.parametrize(
    "method", [None, "get"], ids=["missing-file-review-route", "wrong-file-review-method"]
)
def test_openapi_requires_file_review_post(tmp_path: Path, method: str | None) -> None:
    scenario = Scenario(file_review_method=method)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"缺少路由或方法：{FILE_REVIEW_PATH}" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert [path for _, path, _ in scenario.requests] == ["/health", "/docs", "/openapi.json"]


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(
            200, VALID_FILE_PATH_DETAIL, "无效 PR 文件路径应返回 422", id="unexpected-success"
        ),
        pytest.param(404, VALID_FILE_PATH_DETAIL, "无效 PR 文件路径应返回 422", id="not-found"),
        pytest.param(500, VALID_FILE_PATH_DETAIL, "无效 PR 文件路径应返回 422", id="server-error"),
        pytest.param(503, VALID_FILE_PATH_DETAIL, "无效 PR 文件路径应返回 422", id="rate-limit"),
        pytest.param(422, "错误说明", "detail 应为非空验证错误数组", id="string-detail"),
        pytest.param(422, [], "detail 应为非空验证错误数组", id="empty-detail"),
        pytest.param(422, [{"loc": ["body", "pull_number"]}], "验证错误未指向", id="wrong-field"),
        pytest.param(422, [{"loc": ["file_path"]}], "验证错误未指向", id="missing-body"),
        pytest.param(
            422, [{"loc": ["body", "file_path", "extra"]}], "验证错误未指向", id="nested-loc"
        ),
        pytest.param(422, ["file_path"], "验证错误未指向", id="non-object-error"),
    ],
)
def test_bad_file_path_validation_fails(
    tmp_path: Path, status: int, detail: object, message: str
) -> None:
    scenario = Scenario(file_review_status=status, file_review_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"PR 文件路径验证: HTTP {status}" in result.stdout
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", FILE_REVIEW_PATH, FILE_REVIEW_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        (422, [{"loc": ["body", "pull_number"]}], "验证错误未指向 body.file_path 字段"),
        (200, VALID_FILE_PATH_DETAIL, "无效 PR 文件路径应返回 422"),
    ],
)
def test_optimized_python_still_checks_file_path(
    tmp_path: Path, status: int, detail: object, message: str
) -> None:
    scenario = Scenario(file_review_status=status, file_review_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 1
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", FILE_REVIEW_PATH, FILE_REVIEW_REQUEST)


@pytest.mark.parametrize(
    "method", [None, "get"], ids=["missing-batch-review-route", "wrong-batch-review-method"]
)
def test_openapi_requires_batch_review_post(tmp_path: Path, method: str | None) -> None:
    scenario = Scenario(batch_review_method=method)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"缺少路由或方法：{BATCH_REVIEW_PATH}" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert [path for _, path, _ in scenario.requests] == ["/health", "/docs", "/openapi.json"]


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        pytest.param(
            200, VALID_BATCH_PATHS_DETAIL, "空 PR 文件路径列表应返回 422", id="unexpected-success"
        ),
        pytest.param(404, VALID_BATCH_PATHS_DETAIL, "空 PR 文件路径列表应返回 422", id="not-found"),
        pytest.param(
            500, VALID_BATCH_PATHS_DETAIL, "空 PR 文件路径列表应返回 422", id="server-error"
        ),
        pytest.param(
            503, VALID_BATCH_PATHS_DETAIL, "空 PR 文件路径列表应返回 422", id="rate-limit"
        ),
        pytest.param(422, "错误说明", "detail 应为非空验证错误数组", id="string-detail"),
        pytest.param(422, [], "detail 应为非空验证错误数组", id="empty-detail"),
        pytest.param(422, [{"loc": ["body", "pull_number"]}], "验证错误未指向", id="wrong-field"),
        pytest.param(422, [{"loc": ["file_paths"]}], "验证错误未指向", id="missing-body"),
        pytest.param(422, [{"loc": ["body", "file_paths", 0]}], "验证错误未指向", id="nested-loc"),
        pytest.param(422, ["file_paths"], "验证错误未指向", id="non-object-error"),
    ],
)
def test_bad_batch_path_list_validation_fails(
    tmp_path: Path, status: int, detail: object, message: str
) -> None:
    scenario = Scenario(batch_review_status=status, batch_review_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert f"PR 批量文件路径验证: HTTP {status}" in result.stdout
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", BATCH_REVIEW_PATH, BATCH_REVIEW_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


@pytest.mark.parametrize(
    ("status", "detail", "message"),
    [
        (422, [{"loc": ["body", "pull_number"]}], "验证错误未指向 body.file_paths 字段"),
        (200, VALID_BATCH_PATHS_DETAIL, "空 PR 文件路径列表应返回 422"),
    ],
)
def test_optimized_python_still_checks_batch_path_list(
    tmp_path: Path, status: int, detail: object, message: str
) -> None:
    scenario = Scenario(batch_review_status=status, batch_review_detail=detail)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 1
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", BATCH_REVIEW_PATH, BATCH_REVIEW_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


def test_optimized_python_completes_all_fourteen_checks(tmp_path: Path) -> None:
    scenario = Scenario()
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert SUCCESS_MESSAGE in result.stdout
    assert len(scenario.requests) == 14
    assert scenario.requests[-2:] == [
        ("POST", BATCH_REVIEW_PATH, BATCH_REVIEW_REQUEST),
        ("GET", "/health", None),
    ]
    assert ("POST", "/api/v1/reviews", MUTABLE_DEFAULT_REQUEST) in scenario.requests


@pytest.mark.parametrize("status", [422, 500])
def test_mutable_default_smoke_rejects_error_status(tmp_path: Path, status: int) -> None:
    scenario = Scenario(mutable_default_status=status)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert "可变默认参数 状态码应为 200" in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", "/api/v1/reviews", MUTABLE_DEFAULT_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


@pytest.mark.parametrize(
    ("response_changes", "issue_changes", "message"),
    [
        ({"language": "javascript"}, {}, "language 应为 python"),
        ({"issue_count": True}, {}, "数量与列表不一致"),
        ({"issue_count": 0, "issues": []}, {}, "应恰好包含一个问题"),
        ({"issue_count": 2, "issues": [{}, {}]}, {}, "应恰好包含一个问题"),
        ({"issues": None}, {}, "issues 应为数组"),
        ({}, {"rule_id": "PY_PRINT"}, "rule_id 不正确"),
        ({}, {"severity": "info"}, "severity 不正确"),
        ({}, {"line": 1}, "行号应为 2"),
        ({}, {"line": True}, "行号必须是正整数"),
        ({}, {"line": "2"}, "行号必须是正整数"),
    ],
)
def test_mutable_default_smoke_rejects_wrong_result(
    tmp_path: Path,
    response_changes: dict[str, object],
    issue_changes: dict[str, object],
    message: str,
) -> None:
    body = mutable_default_response()
    issues = body["issues"]
    assert isinstance(issues, list) and isinstance(issues[0], dict)
    issues[0].update(issue_changes)
    body.update(response_changes)
    scenario = Scenario(mutable_default_body=body)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path)

    assert result.returncode == 1
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", "/api/v1/reviews", MUTABLE_DEFAULT_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("rule_id", "PY_PRINT", "rule_id 不正确"), ("line", 1, "行号应为 2")],
)
def test_optimized_python_still_checks_mutable_defaults(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    body = mutable_default_response()
    issues = body["issues"]
    assert isinstance(issues, list) and isinstance(issues[0], dict)
    issues[0][field] = value
    scenario = Scenario(mutable_default_body=body)
    with local_server(scenario) as port:
        result = run_smoke(port, tmp_path, optimized=True)

    assert "-O" in result.args
    assert result.returncode == 1
    assert message in result.stderr
    assert SUCCESS_MESSAGE not in result.stdout
    assert scenario.requests[-1] == ("POST", "/api/v1/reviews", MUTABLE_DEFAULT_REQUEST)
    assert scenario.requests.count(("GET", "/health", None)) == 1
