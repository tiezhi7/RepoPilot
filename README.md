# RepoPilot

RepoPilot 当前是一个可在本地运行的 **Python 静态代码审查后端**，使用 FastAPI 提供 HTTP 接口，使用 Python 标准库 AST（抽象语法树，即代码的结构化表示）检查提交的源码。

项目的长期目标是发展为面向 GitHub 的 AI 代码审查助手；当前应用尚未接入 GitHub API 或 AI 服务。仓库已提供 GitHub Actions 持续集成配置，用于自动检查项目代码。审查过程只解析源码，不执行、导入或求值用户提交的代码。

## 已实现功能

### Day 1：最小可运行 API

- `GET /health`：返回服务健康状态。
- `POST /api/v1/reviews`：接收 Python 源码，返回结构化审查结果。
- 定义请求、问题和响应模型；要求 `code` 为长度 1–20000 的字符串。
- 支持 `PY_EVAL`、`PY_PRINT`、`PY_BARE_EXCEPT` 和 `PY_SYNTAX`。
- 对错误请求返回 HTTP 422，对源码解析错误返回 `PY_SYNTAX` 审查结果。
- 使用 pytest 和 FastAPI `TestClient` 验证接口行为及输入边界。

### Day 2：规则拆分、测试补充与工具配置

- 将三个 AST 节点规则拆为独立函数，命中时返回 `ReviewIssue`，不匹配时返回 `None`。
- 使用统一收集器遍历一次 AST，审查器负责解析、处理解析错误及按行号排序；API 格式保持不变。
- 增加直接调用审查器的测试，验证打印规则、同一行问题的顺序、语法错误行号，以及源码不会实际写文件。
- 增加超长源码的独立接口测试，确认长度超过上限时返回 422。
- 添加 `requirements-dev.txt` 和 `pyproject.toml`，配置 Ruff 与 pytest。

### Day 3：类型检查、覆盖率与持续集成

- 在开发依赖中引入 mypy、pytest-cov，保留已有 Ruff 和 pytest 配置。
- mypy 按 Python 3.12 的类型规则检查 `app`，要求函数类型注解并提醒不明确的返回类型；当前不检查测试目录。
- 使用 pytest-cov 测量 `app` 的覆盖率，补充解析失败、缺失行号、语法错误不返回部分结果，以及无效输入不进入审查器的测试。
- 新增 `.github/workflows/backend-ci.yml`，在 `push` 和 `pull_request` 事件下运行后端检查，使用 Python 3.12 和只读仓库权限。
- 开发依赖增加 `httpx2`，让 Starlette `TestClient` 使用当前推荐的客户端；原有 `httpx` 保留，未通过过滤警告掩盖弃用提示。
- `.gitignore` 忽略 mypy 缓存及覆盖率生成文件。

CI 配置已存在，远程运行结果尚未在本次审查中确认；本地验证记录见下文。

以上是代码和配置中已有的能力；后面的“下一阶段计划”尚未实施。

## 项目结构

```text
RepoPilot/
├── .github/
│   └── workflows/
│       └── backend-ci.yml
├── AGENTS.md
├── .gitignore
├── README.md
└── backend/
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── pyproject.toml
    ├── app/
    │   ├── __init__.py
    │   ├── main.py
    │   ├── schemas.py
    │   ├── reviewer.py
    │   └── rules.py
    └── tests/
        ├── test_api.py
        └── test_reviewer.py
```

- `AGENTS.md`：记录项目范围和编程协作规则。
- `.gitignore`：告诉 Git 哪些文件不需要纳入版本管理。
- `README.md`：介绍项目、目录结构及使用方式。
- `.github/workflows/backend-ci.yml`：定义 GitHub Actions 后端检查任务。
- `backend/requirements.txt`：基础依赖清单，目前包含服务运行包及测试包。
- `backend/requirements-dev.txt`：引入基础依赖清单，再安装 Ruff、mypy、pytest-cov 和测试客户端 httpx2。
- `backend/pyproject.toml`：保存 Ruff、pytest 和 mypy 配置；覆盖率通过 pytest 命令行参数启用。
- `backend/app/__init__.py`：将 `app` 目录标识为 Python 包，便于导入其中的模块。
- `backend/app/main.py`：FastAPI 应用入口，负责接收请求和组织响应。
- `backend/app/schemas.py`：请求和响应的数据模型，负责校验字段类型及长度。
- `backend/app/reviewer.py`：解析源码、处理解析异常、调用规则收集器，并对结果排序。
- `backend/app/rules.py`：三个独立节点规则及只遍历一次 AST 的收集器。
- `backend/tests/test_api.py`：验证接口、审查规则、输入边界及源码不被执行。
- `backend/tests/test_reviewer.py`：直接验证审查器的输出顺序、行号及不执行源码的约定。

## 环境准备

以下命令适用于 Windows PowerShell。CI 使用 Python 3.12，Ruff 和 mypy 的目标版本也都是 3.12；本地验证环境及其结果见下文。静态检查的目标版本不限制服务实际使用的解释器，也不代表已经完成多版本兼容性验证。

从项目根目录 `RepoPilot` 开始，先进入 `backend`。后续安装、启动、测试和质量检查命令都在 `backend` 目录执行；如果已经位于该目录，无需再次执行 `Set-Location backend`。

```powershell
Set-Location backend
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

若已有可用的 `backend/.venv`，可以跳过创建虚拟环境的命令。`.venv` 是本项目的独立 Python 环境，用于隔离依赖；上面的命令直接使用其中的 Python，无需先激活环境。

当前依赖清单的实际关系如下：

| 文件 | 当前内容 |
| --- | --- |
| `requirements.txt` | `fastapi`、`uvicorn[standard]`、`httpx`、`pytest` |
| `requirements-dev.txt` | 通过 `-r requirements.txt` 引入基础清单，再添加 `ruff`、`mypy`、`pytest-cov`、`httpx2` |

仅启动服务时也可以安装基础清单：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

当前 `httpx` 和 `pytest` 仍在基础清单中，尚未完全分离运行依赖与测试依赖。新增的 Ruff、mypy、pytest-cov 和 httpx2 仅在开发清单中，不是当前 API 服务运行所需的依赖。

各包的用途：

- `fastapi`：用于编写后端 HTTP 接口。
- `uvicorn`：用于运行 FastAPI 应用的服务器。
- `httpx`：保留的基础 HTTP 客户端依赖，未在此次迁移中删除。
- `pytest`：用于发现和运行自动化测试。
- `ruff`：检查本项目源码的代码质量和格式，不参与 API 对提交源码的审查。
- `mypy`：在运行前检查参数、返回值等类型是否一致，不替代 API 收到请求时的输入校验。
- `pytest-cov`：为 pytest 提供覆盖率统计和报告。
- `httpx2`：Starlette `TestClient` 当前优先使用的客户端，安装后无需修改原有 `TestClient` 导入。参见 [Starlette 官方说明](https://starlette.dev/testclient/)。

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

同一行的问题保留原 AST 遍历顺序，不保证按列号排列。例如 `print(eval('1 + 1')); print('again')` 当前返回两个 `PY_PRINT`，再返回 `PY_EVAL`。

- 缺少 `code`、类型不是字符串、长度越界、无效 Unicode、请求体缺失或 JSON 格式错误：返回 HTTP 422，`detail` 中包含错误位置、类型和说明，不包含原始输入。
- 源码语法错误：返回 HTTP 200，包含一条 `PY_SYNTAX`；无法建立 AST 时不继续检查其他规则。
- 非法字符或表达式复杂度导致的解析失败也返回 `PY_SYNTAX`；解析器未提供行号时使用第 1 行作为定位起点。

审查流程是：请求校验 → 解析一次源码 → 遍历一次 AST 并检查节点规则 → 按行号排序 → 组织响应。解析失败时不会继续收集其他规则的问题。

## 当前限制与不支持的功能

- **只有 Python 源码字符串审查。** 不支持其他语言、整个仓库、多个文件或代码 diff 的审查；可解析的 Python 语法取决于运行服务的解释器版本。
- **规则只匹配语法结构。** 不追踪函数别名、变量绑定、跨文件关系或 `builtins.eval(...)`、`obj.print(...)` 等属性调用；自定义同名函数也可能被提示。
- **不是完整的 Python 正确性检查。** `PY_SYNTAX` 表示 AST 解析失败，不代表能发现所有编译阶段或运行时错误。注释和普通字符串中的规则名称不会触发问题；字符串插值表达式中的真实调用仍会被遍历。
- **提示需要结合上下文判断。** `print()` 有时是正常输出；`PY_PRINT` 只是提示，并不自动认定代码有缺陷。
- **不提供完整安全分析或安全保证。** 当前规则不是完整的漏洞扫描，也没有沙箱执行、进程隔离或资源配额。源码不被执行，但长度校验和解析异常处理不能替代完整的资源隔离。
- **没有 GitHub 审查业务集成。** 应用不读取仓库或 PR，不处理 webhook，也不自动发布审查评论；GitHub Actions 仅负责验证本项目。
- **没有 AI 审查。** 不调用 LLM（大语言模型）或付费 AI API，不生成 AI 修复建议。
- **没有产品前端、数据库或审查记录持久化。** `/docs` 是 FastAPI 提供的接口调试文档。

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
.\.venv\Scripts\python.exe -m pytest -q
```

`pyproject.toml` 已将测试发现目录设置为 `tests`，并将 `backend` 加入测试导入路径，所以命令无需额外修改 `PYTHONPATH`。`TestClient` 在进程内调用接口，不需要真实 GitHub 或 AI 服务。

现有测试验证健康检查、四条审查规则、结果排序、同一行多个问题、无问题代码、字符串和注释、长度边界、非法请求，以及提交的源码不会被执行。审查器测试使用 pytest 的临时目录确认写文件语句没有实际创建目标文件。

新增的解析器异常测试使用 `monkeypatch` 临时模拟失败，避免使用可能耗尽资源的输入来触发异常；这不等于验证了真实资源耗尽场景。

## 在本地运行全部质量检查

先安装 `requirements-dev.txt`，然后在 `backend` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
```

`pip check` 检查已安装依赖是否冲突；其余四条命令与 CI 中的检查一致。每条命令都应成功，不能只根据最后一条的结果判断所有检查通过。

如需确认没有弃用警告等问题，可将警告视为错误运行同一套测试：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q -W error
```

`-W error` 会让警告导致失败，不会隐藏警告；当前 CI 的测试命令未添加这个参数。

### mypy 类型检查

配置位于 `pyproject.toml` 的 `[tool.mypy]`：目标 Python 3.12，检查范围为 `app`，启用函数注解要求、函数内部检查、未明确类型的返回值提醒及无用忽略提醒。当前没有对 `tests` 启用类型检查，也没有使用全局 `ignore_missing_imports`。

### pytest-cov 覆盖率报告

`--cov=app` 仅测量应用代码，`--cov-report=term-missing` 在终端列出未覆盖的位置。运行会生成 `.coverage` 数据文件，已由 `.gitignore` 忽略。

当前 CI 测量行覆盖率，未添加 `--cov-branch`，也未设置最低覆盖率门槛。若要单独检查分支覆盖率，可在测试命令中增加 `--cov-branch`；它与行覆盖率是不同的统计口径。

### 最近一次本地验证：2026-10-27

环境为 **Windows、Python 3.14.6**。实际运行了上面的依赖、Ruff、mypy 检查，以及带 `-W error` 的完整覆盖率测试：

| 检查项 | 实际结果 |
| --- | --- |
| pytest | 45 passed，0 警告 |
| `app` 行覆盖率 | 100%，报告无未覆盖行 |
| mypy | 5 个源文件，未发现问题 |
| Ruff 代码检查 | 通过 |
| Ruff 格式检查 | 7 个文件符合格式 |
| `pip check` | 未发现依赖冲突 |

这是特定本地环境的验证记录，不代表 Python 3.12、Linux 或 GitHub 云端运行已经通过。100% 行覆盖率也不代表验证了所有输入组合或完整安全性；当前没有性能基准。

## Ruff 代码质量检查与格式检查

先安装 `requirements-dev.txt`。在 `backend` 目录运行代码质量检查：

```powershell
.\.venv\Scripts\python.exe -m ruff check .
```

当前启用 `E4`、`E7`、`E9`、`F`、`I`，检查部分基础代码错误、潜在错误和导入排序；没有启用 Ruff 的全部规则。项目未启用默认自动修复，命令也未添加 `--fix`。

运行格式检查：

```powershell
.\.venv\Scripts\python.exe -m ruff format --check .
```

`--check` 只检查，不改写文件。格式设置来自 `pyproject.toml`，行宽为 100，目标 Python 版本为 3.12。若报告文件需要重新格式化，命令会返回非零退出码，需要另行检查差异；通过检查也不等于业务逻辑一定正确。

## GitHub Actions 后端 CI

工作流文件位于 `.github/workflows/backend-ci.yml`，名称为 **Backend CI**，在以下事件下自动运行：

- `push`：提交被推送到仓库时触发。
- `pull_request`：由工作流监听的 Pull Request 事件触发。

当前未设置分支或路径过滤。工作流在 `ubuntu-latest` 上使用 `actions/checkout@v7` 获取代码、`actions/setup-python@v7` 安装 Python 3.12，然后在 `backend` 目录执行：

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m ruff format --check .
python -m mypy app
python -m pytest --cov=app --cov-report=term-missing -q
```

任务仅申请 `permissions: contents: read`，无需手动配置密钥，不部署项目或发布软件包。任意检查步骤失败都会使任务失败；覆盖率报告目前只用于观察，尚未设置覆盖率下降时单独失败的门槛。

**远程状态：已配置，尚未在本次审查中确认 GitHub Actions 运行结果。** 本地检查通过不能作为远程 CI 已通过的证据。

你后续手动提交并推送工作流及完整后端文件后，可以在仓库页面选择 **Actions → Backend CI → 对应运行记录 → Backend checks**，展开各步骤查看日志。新增源码、依赖和配置文件需要进入 Git 提交，云端才能取得这些内容。参见 [GitHub 查看工作流运行历史](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-workflow-run-history)。

## 下一阶段计划（尚未实施）

优先继续完善现有静态分析后端：

1. 在 GitHub 上实际运行 Python 3.12 CI，核对云端日志后再记录结果。
2. 为每条规则补充独立的命中与不命中测试，以及 Unicode 长度和误报边界的测试。
3. 评估依赖版本锁定、运行依赖与测试依赖分离，并依据实际报告讨论是否增加覆盖率门槛。

GitHub PR 集成、AI 审查、前端和数据库仍属于后续需要单独设计与确认的工作。
