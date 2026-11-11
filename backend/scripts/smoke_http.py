"""通过真实本机 HTTP 检查容器；仅依赖 Python 标准库。"""

import argparse
import http.client
import json
import sys
import time

HOST = "127.0.0.1"
REQUEST_TIMEOUT = 5.0
READY_TIMEOUT = 45.0


class SmokeError(Exception):
    """响应不符合约定时，让脚本以非零状态退出。"""


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是整数") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1–65535 之间")
    return port


def timeout_for(deadline: float | None) -> float:
    if deadline is None:
        return REQUEST_TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("已超过就绪等待期限")
    return min(REQUEST_TIMEOUT, remaining)


def request(
    port: int,
    method: str,
    path: str,
    payload: dict[str, str | int] | None = None,
    deadline: float | None = None,
) -> tuple[int, bytes]:
    # HTTPConnection 直接连接固定地址，不使用代理，也不会自动跟随重定向。
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout_for(deadline))
    response = None
    try:
        connection.connect()
        sock = connection.sock
        if sock is None:
            raise SmokeError("未建立 HTTP 连接")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {} if data is None else {"Content-Type": "application/json"}
        sock.settimeout(timeout_for(deadline))
        connection.request(method, path, body=data, headers=headers)
        sock.settimeout(timeout_for(deadline))
        response = connection.getresponse()
        chunks = []
        while True:
            # 每次读取前重新计算剩余时间，避免重试不断延长就绪期限。
            sock.settimeout(timeout_for(deadline))
            chunk = response.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
        timeout_for(deadline)
        return response.status, b"".join(chunks)
    finally:
        if response is not None:
            response.close()
        connection.close()


def require(condition: bool, message: str) -> None:
    # 不依赖 assert，python -O 也会保留这些检查。
    if not condition:
        raise SmokeError(message)


def json_body(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def health(port: int, name: str, deadline: float | None = None) -> None:
    print(f"CHECK {name}", flush=True)
    status, body = request(port, "GET", "/health", deadline=deadline)
    print(f"{name}: HTTP {status}", flush=True)
    require(status == 200, f"{name} 状态码应为 200")
    require(json_body(body) == {"status": "ok"}, f"{name} 正文不正确")
    print(f"PASS {name}", flush=True)


def wait_ready(port: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    for attempt in range(1, 91):
        if time.monotonic() >= deadline:
            break
        try:
            health(port, "初始健康检查", deadline)
            return
        except (OSError, http.client.HTTPException, ValueError, SmokeError) as error:
            print(f"就绪探测 {attempt} 失败：{error}", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))
    raise SmokeError("服务未在 45 秒就绪期限内通过健康检查")


def review(
    port: int,
    name: str,
    code: str,
    rule_id: str | None = None,
    severity: str | None = None,
) -> None:
    print(f"CHECK {name}", flush=True)
    status, body = request(port, "POST", "/api/v1/reviews", {"code": code})
    print(f"{name}: HTTP {status}", flush=True)
    require(status == 200, f"{name} 状态码应为 200")
    result = json_body(body)
    if not isinstance(result, dict):
        raise SmokeError(f"{name} 应返回 JSON 对象")
    issues = result.get("issues")
    require(result.get("language") == "python", f"{name} language 应为 python")
    if not isinstance(issues, list):
        raise SmokeError(f"{name} issues 应为数组")
    count = result.get("issue_count")
    require(type(count) is int and count == len(issues), f"{name} 数量与列表不一致")
    if rule_id is None:
        require(count == 0 and issues == [], f"{name} 应为零问题")
    else:
        require(count == 1, f"{name} 应恰好包含一个问题")
        issue = issues[0]
        if not isinstance(issue, dict):
            raise SmokeError(f"{name} 问题应为对象")
        require(issue.get("rule_id") == rule_id, f"{name} rule_id 不正确")
        require(issue.get("severity") == severity, f"{name} severity 不正确")
        line = issue.get("line")
        require(type(line) is int and line >= 1, f"{name} 行号必须是正整数")
        if rule_id == "PY_PRINT":
            require(line == 1, f"{name} 行号应为 1")
    print(f"PASS {name}", flush=True)


def run(port: int) -> None:
    wait_ready(port)

    print("CHECK Swagger HTML", flush=True)
    status, body = request(port, "GET", "/docs")
    print(f"Swagger HTML: HTTP {status}", flush=True)
    require(status == 200, "文档状态码应为 200")
    require(b"SwaggerUIBundle" in body and b"swagger-ui" in body, "缺少 Swagger UI HTML")
    print("PASS Swagger HTML（未加载 CDN）", flush=True)

    print("CHECK OpenAPI", flush=True)
    status, body = request(port, "GET", "/openapi.json")
    print(f"OpenAPI: HTTP {status}", flush=True)
    require(status == 200, "OpenAPI 状态码应为 200")
    document = json_body(body)
    require(isinstance(document, dict), "OpenAPI 应为 JSON 对象")
    paths = document.get("paths") if isinstance(document, dict) else None
    require(isinstance(paths, dict), "OpenAPI 缺少 paths 对象")
    expected = {
        "/health": "get",
        "/api/v1/reviews": "post",
        "/api/v1/github/repository": "post",
        "/api/v1/github/file-review": "post",
        "/api/v1/github/pull-request": "post",
    }
    for path, method in expected.items():
        route = paths.get(path) if isinstance(paths, dict) else None
        require(isinstance(route, dict) and method in route, f"缺少路由或方法：{path}")
    print("PASS OpenAPI 五个业务路由", flush=True)

    review(port, "正常源码", "x = 1\n")
    review(port, "print 调用", "print('day9')\n", "PY_PRINT", "info")
    review(port, "非法语法", "def broken(:\n", "PY_SYNTAX", "error")

    print("CHECK 空 code", flush=True)
    status, body = request(port, "POST", "/api/v1/reviews", {"code": ""})
    print(f"空 code: HTTP {status}", flush=True)
    require(status == 422, "空 code 应返回 422")
    result = json_body(body)
    details = result.get("detail") if isinstance(result, dict) else None
    require(isinstance(details, list) and len(details) > 0, "detail 应为非空验证错误数组")
    require(
        isinstance(details, list)
        and any(
            isinstance(error, dict)
            and isinstance(error.get("loc"), list)
            and len(error["loc"]) > 0
            and error["loc"][-1] == "code"
            for error in details
        ),
        "验证错误未指向 code 字段",
    )
    print("PASS 空 code 验证", flush=True)

    # 此字符串仅通过 JSON 提交，验收脚本自身不执行它。
    review(port, "源码不执行", 'raise RuntimeError("DAY9_MUST_NOT_EXECUTE")\n')

    print("CHECK PR 编号验证", flush=True)
    status, body = request(
        port,
        "POST",
        "/api/v1/github/pull-request",
        {"repository_url": "https://github.com/example/repo", "pull_number": 0},
    )
    print(f"PR 编号验证: HTTP {status}", flush=True)
    require(status == 422, "无效 PR 编号应返回 422")
    result = json_body(body)
    details = result.get("detail") if isinstance(result, dict) else None
    require(isinstance(details, list) and len(details) > 0, "detail 应为非空验证错误数组")
    require(
        isinstance(details, list)
        and any(
            isinstance(error, dict) and error.get("loc") == ["body", "pull_number"]
            for error in details
        ),
        "验证错误未指向 body.pull_number 字段",
    )
    print("PASS PR 编号验证", flush=True)

    health(port, "最后健康检查")
    print("PASS 全部十项真实 HTTP 检查", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查本机容器端口的十项真实 HTTP 行为")
    parser.add_argument("--port", required=True, type=port_number, help="宿主机端口，1–65535")
    args = parser.parse_args()
    try:
        run(args.port)
    except (OSError, http.client.HTTPException, ValueError, SmokeError) as error:
        print(f"FAIL HTTP 冒烟检查：{error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
