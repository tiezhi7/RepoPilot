# RepoPilot

RepoPilot 当前是一个基于 FastAPI 的 Python 后端，提供两项独立能力：使用 Python 标准库 AST（抽象语法树，即代码的结构化表示）静态审查提交的 Python 源码，以及使用 HTTPX 查询 GitHub 公开仓库的元数据，例如仓库名称、描述和 stars 数量。

项目的长期目标是发展为面向 GitHub 的 AI 代码审查助手。当前已实现 GitHub REST API 查询客户端和路由，尚未接入 AI，也不会自动下载仓库源码进行审查。源码审查过程只解析代码，不执行、导入或求值用户提交的代码。

仓库已提供 GitHub Actions 持续集成配置。**当前 Day 4 的真实 GitHub 网络访问和远程 CI 尚未验证**；实现检查与离线测试通过不代表这两项验证已完成。

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

### Day 4：公开仓库元数据查询

- 新增 `GitHubRepositoryRequest`、`GitHubRepositoryResponse` 和纯 URL 解析函数，保留原有审查模型。
- 新增同步 `GitHubRepositoryClient`，复用原有 HTTPX 依赖，固定请求 `api.github.com`，配置超时并禁止跟随重定向。
- 将 GitHub 的 `stargazers_count` 映射为 `stars`，只返回约定的七个字段，并严格校验类型及必填字段。
- 新增 `POST /api/v1/github/repository`，通过 FastAPI `Depends` 注入客户端；每次请求结束时关闭客户端，异常路径也会清理资源。
- 区分不存在、明确限流、普通拒绝访问、重定向、无效响应、超时和网络错误，向调用方返回固定错误说明。
- 增加模型、客户端和 API 集成测试，使用 `MockTransport` 模拟 GitHub，并阻断真实网络访问；包含旧接口回归和资源关闭检查。

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
    │   ├── github_client.py
    │   ├── reviewer.py
    │   └── rules.py
    └── tests/
        ├── test_api.py
        ├── test_github_schemas.py
        ├── test_github_client.py
        ├── test_github_api.py
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
- `backend/app/github_client.py`：GitHub 同步 HTTP 客户端、七字段映射、客户端异常及连接生命周期管理。
- `backend/app/reviewer.py`：解析源码、处理解析异常、调用规则收集器，并对结果排序。
- `backend/app/rules.py`：三个独立节点规则及只遍历一次 AST 的收集器。
- `backend/tests/test_api.py`：验证接口、审查规则、输入边界及源码不被执行。
- `backend/tests/test_github_schemas.py`：验证仓库 URL 安全规则及请求、响应模型。
- `backend/tests/test_github_client.py`：使用模拟响应验证请求目标、超时配置、上游错误、数据校验及连接关闭。
- `backend/tests/test_github_api.py`：验证新路由、错误映射、依赖覆盖清理、实际依赖生命周期及原有接口兼容性。
- `backend/tests/test_reviewer.py`：直接验证审查器的输出顺序、行号及不执行源码的约定。

## 环境准备

以下命令适用于 Windows PowerShell。CI 使用 Python 3.12，Ruff 和 mypy 的目标版本也都是 3.12；本地验证环境及其结果见下文。静态检查的目标版本不限制服务实际使用的解释器，也不代表已经完成多版本兼容性验证。

从项目根目录 `RepoPilot` 开始，先进入 `backend`。后续安装、启动、测试和质量检查命令都在 `backend` 目录执行；如果已经位于该目录，无需再次执行 `Set-Location backend`。

```powershell
Set-Location backend
python --version
python -m venv .venv
```

若已有可用的 `backend/.venv`，跳过创建命令，不要覆盖现有环境。`python -m venv` 使用当前 `python` 对应的解释器；想与 CI 一致时，应确认所选解释器为 Python 3.12。静态检查配置不会自动切换解释器版本。

`.venv` 是本项目的独立 Python 环境，用于隔离依赖。可在当前 PowerShell 窗口中激活：

```powershell
.\.venv\Scripts\Activate.ps1
python --version
```

激活后，`python` 指向该虚拟环境；执行 `deactivate` 可退出。若激活脚本不能运行，也可直接使用下面的虚拟环境解释器，无需修改系统执行策略。后续命令统一采用这种显式路径：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

这里的路径是 `.\.venv`，不是 `..venv`。安装开发清单会同时安装基础依赖和检查工具；本轮文档验收不执行安装或升级。

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
- `httpx`：GitHub 查询功能实际使用的同步 HTTP 客户端。
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

### POST /api/v1/github/repository

接收 `GitHubRepositoryRequest`，查询指定公开仓库的元数据。请求体只允许 `repository_url`：它必须是真正的字符串，长度为 1–2048 个字符；不接受额外的 `token`、`api_url` 等字段。

```json
{"repository_url": "https://github.com/psf/requests"}
```

只接受仓库主页地址，也允许一个末尾 `/`。域名忽略大小写，owner 和 repo 的大小写保留；不接受克隆地址或仓库内页面。

成功时返回 HTTP 200 和 `GitHubRepositoryResponse`，始终包含以下七个字段：

| 字段 | JSON 类型 | 说明 |
| --- | --- | --- |
| `full_name` | 字符串 | 仓库完整名称，如 `psf/requests` |
| `description` | 字符串或 `null` | 仓库描述 |
| `language` | 字符串或 `null` | GitHub 返回的主要语言 |
| `stars` | 非负整数 | 由 GitHub 的 `stargazers_count` 映射而来 |
| `default_branch` | 字符串 | 默认分支名称 |
| `archived` | 布尔值 | 是否已归档 |
| `html_url` | 字符串 | GitHub 返回的仓库网页地址 |

允许 `null` 不代表字段可以省略。客户端要求上游包含全部必要字段，拒绝错误类型和负数 stars；额外的上游字段不会直接返回。本接口不会读取源码、触发 AST 审查或发布 GitHub 评论。这里没有展示假定的实时 stars 数量，也未实际执行示例请求。

### GitHub 查询的错误响应

输入验证沿用现有自定义处理器。例如，请求体为 `{}` 时返回 HTTP 422：

```json
{
  "detail": [
    {"loc": ["body", "repository_url"], "type": "missing", "msg": "Field required"}
  ]
}
```

`loc` 表示错误位置，`type` 表示错误类别，`msg` 是说明；错误项不包含原始输入或内部校验上下文。客户端错误使用固定的 `detail` 字符串，例如 HTTP 404：

```json
{"detail": "仓库不存在或不可公开访问。"}
```

| HTTP 状态 | 实际含义 |
| --- | --- |
| `200` | 请求成功；源码审查发现问题（包括 `PY_SYNTAX`）仍属于正常审查结果 |
| `422` | 请求体、字段类型、长度或 GitHub URL 验证失败，不发送 GitHub HTTP 请求 |
| `404` | GitHub 返回 404，仓库不存在或不可匿名公开访问 |
| `502` | 普通 GitHub 403、重定向、其他非预期上游状态、无效响应数据或网络请求错误 |
| `503` | GitHub 429，或有明确证据属于限流的 GitHub 403 |
| `504` | HTTPX 连接、读取、写入或连接池等待超时 |

**普通 403 不会自动被认定为限流。** 只有 `X-RateLimit-Remaining` 去除首尾空白后为 `0`，或者消息明确包含 `API rate limit` / `secondary rate limit`（忽略大小写），才将 403 归为限流。GitHub 的其他状态也不会原样透传。

限流时，HTTP 503 的响应为：

```json
{"detail": "GitHub 请求受到限流，请稍后重试。"}
```

其余固定说明包括 `GitHub 拒绝访问该仓库。`、`GitHub 返回了重定向，请使用仓库当前的主页地址。`、`GitHub 返回了无效或非预期的响应。`、`GitHub 网络请求失败。` 和 `GitHub 请求超时。`。路由不直接返回客户端异常字符串、上游 JSON 或请求头。

## GitHub 查询的安全与资源管理

- **校验原始字符串后再解析。** 拒绝空白、控制字符、百分号编码和反斜杠，防止解析器清理字符后产生歧义。
- **协议和域名精确匹配。** 仅接受 HTTPS 和 `github.com`，不允许用户信息、显式端口、查询参数或片段，包括空的 `?`、`#`。
- **路径采用明确的字符规则。** 只允许 `/owner/repo` 和最多一个末尾斜杠。owner 仅允许 ASCII 字母、数字及单个内部连字符；repo 仅允许 ASCII 字母、数字、点、下划线和连字符，不能是独立的 `.` 或 `..`，也不能以 `.git` 结尾（忽略大小写）。
- **固定 HTTP 请求目标。** 客户端只用已验证的 owner、repo 构造 `https://api.github.com/repos/{owner}/{repo}`，不请求用户提交的完整 URL，不接受用户指定的 API 地址、请求头或认证信息。这用于防止 SSRF，即诱导服务器请求任意地址。
- **不跟随重定向，保持 TLS 证书验证。** 不访问上游 `Location` 指定的目标。
- **匿名读取公开信息。** 不需要配置 GitHub Token，也不发送 `Authorization`。匿名请求受到 GitHub API 限额约束，多个调用者可能共享服务器来源 IP 的额度；具体规则以 [GitHub 官方限额文档](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 为准。
- **设置明确超时。** connect 为 3 秒，read、write、pool 各为 5 秒；这些是不同网络阶段的超时，不是整个请求总耗时最多 5 秒。
- **清理连接。** 每次 API 请求通过依赖创建客户端，成功或异常结束都关闭；客户端也支持 `with`、`close()` 和公开的 `is_closed`。模块导入时不创建长期存活的 HTTP 客户端。
- **限制错误信息。** 上游数据必须经过模型验证，失败时返回固定说明；原始错误正文和底层异常文本不会直接透传给 API 调用方。

URL 校验函数本身不进行 DNS 或 HTTP 请求；仓库是否存在、是否可公开访问，需要后续实际 GitHub 响应确认。Python 审查器只做 AST 分析，不执行用户提交的源码。

## 当前限制与不支持的功能

- **只有 Python 源码字符串审查。** 不支持其他语言、整个仓库、多个文件或代码 diff 的审查；可解析的 Python 语法取决于运行服务的解释器版本。
- **规则只匹配语法结构。** 不追踪函数别名、变量绑定、跨文件关系或 `builtins.eval(...)`、`obj.print(...)` 等属性调用；自定义同名函数也可能被提示。
- **不是完整的 Python 正确性检查。** `PY_SYNTAX` 表示 AST 解析失败，不代表能发现所有编译阶段或运行时错误。注释和普通字符串中的规则名称不会触发问题；字符串插值表达式中的真实调用仍会被遍历。
- **提示需要结合上下文判断。** `print()` 有时是正常输出；`PY_PRINT` 只是提示，并不自动认定代码有缺陷。
- **不提供完整安全分析或安全保证。** 当前规则不是完整的漏洞扫描，也没有沙箱执行、进程隔离或资源配额。源码不被执行，但长度校验和解析异常处理不能替代完整的资源隔离。
- **GitHub 功能仅限公开仓库元数据。** 不下载文件、不审查整个远程仓库、不读取 PR 或 diff，不处理 webhook，也不自动发布评论；GitHub Actions 仅负责验证本项目。
- **GitHub 客户端没有重试、缓存或认证。** 当前也没有调用方鉴权、应用级请求限流或上游响应体大小配额，不属于完整的生产环境防护。
- **网络配置固定。** `trust_env=False`，不会自动采用环境代理或自定义证书变量；需要代理或企业证书的环境可能无法直接连接。真实网络、DNS、TLS 握手和远程 CI 尚未通过本轮验收。
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

上面的两个接口不需要 GitHub 网络。访问 `/docs` 也不会自动查询 GitHub；在文档页面执行仓库查询接口会发出真实 GitHub 请求，属于另行进行的实网验证，本轮未执行。

## 运行自动化测试

在 `backend` 目录执行，无需另行启动服务：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

`pyproject.toml` 已将测试发现目录设置为 `tests`，并将 `backend` 加入测试导入路径，所以命令无需额外修改 `PYTHONPATH`。`TestClient` 在进程内调用接口，不需要真实 GitHub 或 AI 服务。

现有测试验证健康检查、四条审查规则、结果排序、同一行多个问题、无问题代码、字符串和注释、长度边界、非法请求，以及提交的源码不会被执行。审查器测试使用 pytest 的临时目录确认写文件语句没有实际创建目标文件。

GitHub 模型测试覆盖合法 URL、伪装域名、路径及字符边界、严格类型和额外字段。客户端测试通过 HTTPX `MockTransport` 返回模拟响应或主动抛出超时、连接异常；它们不会等待真实网络超时。API 测试使用 FastAPI `TestClient`，覆盖七字段响应、422 格式、各类错误映射、异常文本脱敏和旧接口回归。

测试阻断真实网络；依赖覆盖在结束时恢复。资源测试既检查模拟依赖，也通过替换构造器的传输层执行真实的生产依赖，验证每次请求独立创建并关闭客户端。这些测试不验证真实 GitHub 数据、DNS、TLS 握手或实际服务延迟。

新增的解析器异常测试使用 `monkeypatch` 临时模拟失败，避免使用可能耗尽资源的输入来触发异常；这不等于验证了真实资源耗尽场景。

## 在本地运行全部质量检查

先安装 `requirements-dev.txt`，然后在 `backend` 目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app
```

以上四条与 CI 使用相同的检查内容；CI 的执行顺序为 Ruff、格式、mypy、pytest。本地命令显式指定虚拟环境解释器，CI 则由 setup-python 选择 Python。每条命令都应成功，不能只根据最后一条的结果判断所有检查通过。

可选运行 `.\.venv\Scripts\python.exe -m pip check` 检查已安装依赖冲突；它不是当前 CI 的检查步骤。

如需确认没有弃用警告等问题，可将警告视为错误运行同一套测试：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q -W error
```

`-W error` 会让警告导致失败，不会隐藏警告；当前 CI 的测试命令未添加这个参数。

### mypy 类型检查

配置位于 `pyproject.toml` 的 `[tool.mypy]`：目标 Python 3.12，检查范围为 `app`，启用函数注解要求、函数内部检查、返回 `Any` 提醒及无用忽略提醒。当前没有对 `tests` 启用类型检查，也没有使用全局 `ignore_missing_imports`。

### pytest-cov 覆盖率报告

`--cov=app` 仅测量应用代码，`--cov-report=term-missing` 在终端列出未覆盖的位置。运行会生成 `.coverage` 数据文件，已由 `.gitignore` 忽略。

当前 CI 测量行覆盖率，未添加 `--cov-branch`，也未设置最低覆盖率门槛。若要单独检查分支覆盖率，可在测试命令中增加 `--cov-branch`；它与行覆盖率是不同的统计口径。

### Day 4 本地验收记录

2026-10-29 在现有 Windows 虚拟环境中重新运行上述四项检查，实际解释器为 Python 3.14.6，结果如下。这是本轮输出，不是引用 Day 3 或第三批的历史结果。

| 检查 | 本轮实际结果 | 退出码 |
| --- | --- | --- |
| pytest 与覆盖率 | `335 passed in 2.40s`；`app` 共 197 条可执行语句，未覆盖 0 条，行覆盖率 100% | `0` |
| Ruff 代码检查 | `All checks passed!` | `0` |
| Ruff 格式检查 | `11 files already formatted` | `0` |
| mypy | `Success: no issues found in 6 source files` | `0` |

测试输出没有列出警告；本轮使用普通覆盖率命令，没有添加可选的 `-W error`。检查过程中没有安装依赖、自动修复或格式化文件，也没有执行真实 GitHub API 请求。

本地通过不能证明 Python 3.12、Linux 或 GitHub 云端已经通过。100% 行覆盖率也不代表验证了所有输入组合或分支；当前没有性能基准。

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

**Day 4 远程状态：CI 已配置，当前变更尚未推送，远程运行未验证。** 本地离线检查通过不能作为远程 CI 或真实 GitHub API 访问已通过的证据。

你后续手动提交并推送工作流及完整后端文件后，可以在仓库页面选择 **Actions → Backend CI → 对应运行记录 → Backend checks**，展开各步骤查看日志。新增源码、依赖和配置文件需要进入 Git 提交，云端才能取得这些内容。参见 [GitHub 查看工作流运行历史](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-workflow-run-history)。

## 下一阶段计划（尚未实施）

优先完成当前 Day 4 成果的外部验收，再评估下一阶段：

1. 经单独授权后，提交并推送完整 Day 4 变更，在 GitHub 核对 Python 3.12 CI 日志。
2. 经单独授权后，进行一次公开仓库的匿名实网核验，记录成功、网络配置或额度限制的真实情况，不将其加入离线自动化测试。
3. 评估依赖版本锁定、运行依赖与测试依赖分离、应用级限流及缓存，再依据需求和测试报告讨论覆盖率门槛。

GitHub PR 集成、远程源码审查、AI 审查、前端和数据库仍属于后续需要单独设计与确认的工作。
