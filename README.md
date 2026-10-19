# RepoPilot

RepoPilot 的长期目标是成为面向 GitHub 的可靠 AI 代码审查助手，帮助开发者发现代码问题、理解原因并改进代码。

## Day 1 目标与当前进度

Day 1 使用 Python 和 FastAPI 搭建后端，通过 AST（抽象语法树，即代码的结构化表示）静态分析 Python 源码。审查过程只读取和分析源码，不执行用户提交的代码。

**当前已实现最小可运行 API：** 健康检查、Python 源码审查、请求校验及自动化测试。

Day 1 不包含前端、数据库、GitHub API 集成或 LLM（大语言模型）接口，也不调用付费 AI API。

## 项目结构

```text
RepoPilot/
├── AGENTS.md
├── .gitignore
├── README.md
└── backend/
    ├── requirements.txt
    ├── app/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── schemas.py
    │   └── reviewer.py
    └── tests/
        └── test_api.py
```

- `AGENTS.md`：记录项目范围和编程协作规则。
- `.gitignore`：告诉 Git 哪些文件不需要纳入版本管理。
- `README.md`：介绍项目、目录结构及使用方式。
- `backend/requirements.txt`：列出后端所需的第三方 Python 包。
- `backend/app/__init__.py`：将 `app` 目录标识为 Python 包，便于导入其中的模块。
- `backend/app/main.py`：FastAPI 应用入口，负责接收请求和组织响应。
- `backend/app/schemas.py`：请求和响应的数据模型，负责校验字段类型及长度。
- `backend/app/reviewer.py`：独立的 AST 静态分析逻辑，返回按行号排序的问题列表。
- `backend/tests/test_api.py`：验证接口、审查规则、输入边界及源码不被执行。

## 环境准备

以下命令适用于 Windows PowerShell。从项目根目录 `RepoPilot` 开始，先进入 `backend`，后续命令均在该目录执行。需要 Python 3.10 或更新版本。

```powershell
Set-Location backend
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

若已有可用的 `backend/.venv`，可以跳过创建虚拟环境的命令。`.venv` 是本项目的独立 Python 环境，用于隔离依赖；上面的命令直接使用其中的 Python，无需先激活环境。`requirements.txt` 中包含：

- `fastapi`：用于编写后端 HTTP 接口。
- `uvicorn`：用于运行 FastAPI 应用的服务器。
- `httpx`：HTTP 客户端，配合 FastAPI 的 `TestClient` 测试接口。
- `pytest`：用于发现和运行自动化测试。

## 运行后端

在 `backend` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

`app.main:app` 表示加载 `app/main.py` 中名为 `app` 的对象；`--reload` 用于开发时自动重新加载代码。按 `Ctrl+C` 停止服务。

启动成功后可访问：

- 健康检查：`http://127.0.0.1:8000/health`
- 可交互的接口文档：`http://127.0.0.1:8000/docs`

## 接口与审查规则

### GET /health

返回 HTTP 200 和 `{"status": "ok"}`。

### POST /api/v1/reviews

请求体是 JSON，只需提供 `code` 字符串，长度为 1–20000 个字符。当前仅按 Python 语法分析，不提供语言切换选项；空白字符也计入长度。

```json
{"code": "print(123)"}
```

响应示例：

```json
{
  "language": "python",
  "issue_count": 1,
  "issues": [
    {
      "rule_id": "PY_PRINT",
      "severity": "info",
      "line": 1,
      "message": "发现 print() 调用；请确认是否需要保留调试输出。"
    }
  ]
}
```

| 规则 | 触发条件 | severity（级别） |
| --- | --- | --- |
| `PY_EVAL` | 直接调用 `eval(...)` | `error`：错误或高风险 |
| `PY_PRINT` | 直接调用 `print(...)` | `info`：提示 |
| `PY_BARE_EXCEPT` | 使用不指定异常类型的 `except:` | `warning`：警告 |
| `PY_SYNTAX` | `ast.parse` 解析源码失败 | `error`：错误 |

`line` 从 1 开始，结果按行号升序排列，`issue_count` 等于 `issues` 的条数。没有匹配规则时返回空列表和计数 0。

- 缺少 `code`、类型不是字符串、长度越界、无效 Unicode、请求体缺失或 JSON 格式错误：返回 HTTP 422，`detail` 中包含错误位置、类型和说明，不包含原始输入。
- 源码语法错误：返回 HTTP 200，包含一条 `PY_SYNTAX`；无法建立 AST 时不继续检查其他规则。
- 非法字符或表达式复杂度导致的解析失败也返回 `PY_SYNTAX`；解析器未提供行号时使用第 1 行作为定位起点。

当前检查只依据语法结构，不分析变量实际绑定的对象。因此不追踪别名或 `obj.print(...)` 等属性调用；自定义同名函数也可能被提示。注释和普通字符串中的 `eval`、`print` 不会触发规则。`PY_SYNTAX` 表示 AST 解析失败，不代表能发现所有运行时或编译阶段错误。

## 手动验证

保持服务运行，在另一个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health'

$reviewResult = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:8000/api/v1/reviews' `
    -ContentType 'application/json' `
    -Body '{"code":"print(123)"}'
$reviewResult | ConvertTo-Json -Depth 5
```

第一个请求应返回 `status: ok`；第二个应返回 `language: python`、`issue_count: 1` 和一条 `PY_PRINT` 提示。也可在 `/docs` 页面直接试用接口。

## 运行自动化测试

在 `backend` 目录执行，无需另行启动服务：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

测试覆盖健康检查、四条审查规则、嵌套代码的结果排序、同一行的多个问题、无问题代码、字符串和注释、长度边界、非法请求和源码不被执行。

## 下一步

在 `/docs` 中分别提交正常代码、包含问题的代码和语法错误代码，确认每类响应都能理解。
