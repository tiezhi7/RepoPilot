# RepoPilot

RepoPilot 当前是一个基于 FastAPI 的 Python 后端，支持健康检查、提交 Python 源码审查、GitHub 公开仓库元数据查询，以及公开仓库中单个 Python 文件的静态审查。审查使用 Python 标准库 AST（抽象语法树，即代码的结构化表示），GitHub 查询和文件读取使用 HTTPX。

项目的长期目标是发展为面向 GitHub 的 AI 代码审查助手。当前尚未接入 AI；调用文件审查接口时，只读取默认分支中指定的一个受限 Python 文件，不扫描整个仓库。源码只作为 AST 分析的数据，不执行、导入或保存。

仓库已提供 GitHub Actions 持续集成配置。此前已核实：Day 5 提交已推送，Backend CI #4 成功，并在 2026-10-30 完成最小应用实网验收，详见下文历史记录。Day 6 已分离依赖声明、生成四份平台锁，并完成 Windows 与 Debian Linux 全新环境验收。**本批 Day 6 第六批只修改 README 和 CI 配置并做静态检查，没有重跑安装或测试、查询 CI 或执行实网请求。Ubuntu CI 已配置为使用 Linux 开发锁，但尚无对应的 Day 6 远程运行结果。** 已有实网成功记录不代表网络始终稳定，也不等于外部 HTTP 访问或部署验收通过。

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

### Day 5：单个公开 Python 文件审查

- 新增 `GitHubFileReviewRequest`、`GitHubFileReviewResponse` 和纯文件路径校验函数，复用仓库 URL 验证和原有 `ReviewResponse`。
- 扩展同步客户端的 `fetch_python_file`，只请求固定 Contents API（GitHub 的仓库文件内容接口），读取默认分支指定文件，不接受 `ref`、`branch` 或 `commit` 参数。
- 流式检查文件响应体大小，验证文件身份、类型、编码、声明大小和实际大小，严格解码 Base64 与 UTF-8；允许空文件。
- 新增 `POST /api/v1/github/file-review`，将读取的完整字符串原样交给现有审查器一次，返回文件身份和嵌套审查结果，不返回完整源码。
- 区分请求字段验证、文件业务限制、上游数据异常及网络错误；保留已有接口的行为、问题行号和排序。
- 新增路径与模型测试、文件客户端测试、API 集成测试，离线验证安全边界、不执行源码、错误说明及成功和异常时的资源释放。

### Day 6：依赖分离、平台锁与环境复现

- `requirements.txt` 仅声明 FastAPI、Uvicorn 和 HTTPX；pytest 的原声明移入 `requirements-dev.txt`，版本范围不变。
- 开发清单继续通过 `-r requirements.txt` 引入运行依赖，保留原有工具声明；开发清单合并后的直接依赖声明集合不变。
- 第一批当时只调整声明归属；第二至第五批随后生成 Windows / CPython 3.14 和 Linux / CPython 3.12 的运行锁、开发锁，并分别完成全新环境实际安装、版本核对、`pip check`、运行冒烟及开发检查。
- 锁文件固定完整依赖版本及 SHA-256 哈希；两平台的条件依赖分别由实际目标环境解析，开发锁包含同平台运行锁的全部包且共有版本一致。
- 第六批将后端 CI 配置为 Ubuntu 24.04 / Python 3.12.16，在独立虚拟环境中严格安装 Linux 开发锁，再执行依赖兼容性和原四项质量检查。此处说明配置已完成，远程运行仍待后续验收。

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
    ├── locks/
    │   ├── windows-py314-runtime.txt
    │   ├── windows-py314-dev.txt
    │   ├── linux-py312-runtime.txt
    │   └── linux-py312-dev.txt
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
        ├── test_github_file_schemas.py
        ├── test_github_file_client.py
        ├── test_github_file_api.py
        └── test_reviewer.py
```

- `AGENTS.md`：记录项目范围和编程协作规则。
- `.gitignore`：告诉 Git 哪些文件不需要纳入版本管理。
- `README.md`：介绍项目、目录结构及使用方式。
- `.github/workflows/backend-ci.yml`：定义 GitHub Actions 后端检查任务。
- `backend/requirements.txt`：维护 FastAPI、Uvicorn 和 HTTPX 三项运行直接依赖的版本范围，是生成运行锁的输入。
- `backend/requirements-dev.txt`：通过 `-r requirements.txt` 引入运行声明，再维护 pytest、Ruff、mypy、pytest-cov 和测试客户端 httpx2 的版本范围，是生成开发锁的输入。
- `backend/locks/windows-py314-runtime.txt`、`windows-py314-dev.txt`：Windows AMD64 / CPython 3.14.6 的完整运行、开发依赖及哈希。
- `backend/locks/linux-py312-runtime.txt`、`linux-py312-dev.txt`：Debian 12 / x86_64 / CPython 3.12.16 的完整运行、开发依赖及哈希；其中开发锁也是本次准备接入 Ubuntu CI 的安装入口。
- `backend/pyproject.toml`：保存 Ruff、pytest 和 mypy 配置；覆盖率通过 pytest 命令行参数启用。
- `backend/app/__init__.py`：将 `app` 目录标识为 Python 包，便于导入其中的模块。
- `backend/app/main.py`：FastAPI 应用入口，负责接收请求和组织响应。
- `backend/app/schemas.py`：请求和响应的数据模型、仓库 URL 与相对 Python 文件路径验证。
- `backend/app/github_client.py`：GitHub 同步 HTTP 客户端、元数据七字段映射、受限文件读取与解码、客户端异常及连接生命周期管理。
- `backend/app/reviewer.py`：解析源码、处理解析异常、调用规则收集器，并对结果排序。
- `backend/app/rules.py`：三个独立节点规则及只遍历一次 AST 的收集器。
- `backend/tests/test_api.py`：验证接口、审查规则、输入边界及源码不被执行。
- `backend/tests/test_github_schemas.py`：验证仓库 URL 安全规则及请求、响应模型。
- `backend/tests/test_github_client.py`：使用模拟响应验证请求目标、超时配置、上游错误、数据校验及连接关闭。
- `backend/tests/test_github_api.py`：验证仓库元数据路由、错误映射、依赖覆盖清理、实际依赖生命周期及原有接口兼容性。
- `backend/tests/test_github_file_schemas.py`：验证文件路径字符、长度、原始值保留，以及文件审查请求和嵌套响应模型。
- `backend/tests/test_github_file_client.py`：使用模拟传输和可追踪字节流验证文件读取、大小边界、Base64/UTF-8 解码、错误分类及流关闭。
- `backend/tests/test_github_file_api.py`：使用真实客户端和审查器搭配模拟上游，验证文件路由衔接、输入验证、错误映射、源码不执行、资源释放及旧接口回归。
- `backend/tests/test_reviewer.py`：直接验证审查器的输出顺序、行号及不执行源码的约定。

## 环境准备

依赖分为两层：`requirements.txt` / `requirements-dev.txt` 维护直接依赖的版本范围；`locks/` 中四份锁固定各目标环境的完整直接、间接依赖及哈希。**复现已验证环境时安装对应锁文件**，范围声明用于维护和重新解析，不作为当前 CI 的项目依赖安装入口。

| 文件（相对于 `backend`） | 职责与目标 |
| --- | --- |
| `requirements.txt` | 三项运行直接依赖的版本范围：`fastapi`、`uvicorn[standard]`、`httpx` |
| `requirements-dev.txt` | 引入运行声明，再添加 `pytest`、`ruff`、`mypy`、`pytest-cov`、`httpx2` 五项开发直接依赖 |
| `locks/windows-py314-runtime.txt` | Windows AMD64 / CPython 3.14.6，仅运行服务，22 个锁定包 |
| `locks/windows-py314-dev.txt` | 同一 Windows 目标，包含运行依赖和开发工具，39 个锁定包 |
| `locks/linux-py312-runtime.txt` | Debian 12 / x86_64 / glibc 2.36 / CPython 3.12.16，仅运行服务，23 个锁定包 |
| `locks/linux-py312-dev.txt` | 同一 Debian 目标，包含运行依赖和开发工具，39 个锁定包；Ubuntu 24.04 / Python 3.12.16 CI 目标待远程验证 |

Linux 锁在实际 Debian 容器中生成并验证。基础镜像完整摘要为：

```text
python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
```

Ruff 和 mypy 的目标版本仍是 Python 3.12。静态检查配置不会自动切换解释器，也不代表任意 Python 版本或 Linux 发行版均已验证。安装前先确认解释器版本、架构与所选锁匹配；本次 Ubuntu CI 的目标固定为 24.04 / x64 / Python 3.12.16，其运行结果不能由 Debian 验收推导。

### Windows PowerShell：CPython 3.14.6 / AMD64

从项目根目录进入 `backend`；已在该目录则不必再次进入。以下示例要求当前 `python` 已指向 **CPython 3.14.6 / AMD64**，先检查输出，版本或架构不符时不要继续按本示例安装。

```powershell
Set-Location backend
python --version
python -c "import platform; print(platform.machine())"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --index-url https://pypi.org/simple pip==26.1.2
```

`.venv` 是独立虚拟环境。已有 `.venv` 时不要覆盖；要重新验证依赖分离，应另选干净目录创建环境。下面按用途**只选择一条安装命令**，运行环境与开发环境的隔离验收使用两个全新环境：

```powershell
# 开发：包含运行依赖、测试和质量工具
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r locks/windows-py314-dev.txt
```

```powershell
# 仅运行服务：在独立的全新环境中选择运行锁
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r locks/windows-py314-runtime.txt
```

这里是 `.\.venv`，不是 `..venv`。命令显式指定解释器，不需要激活；如需交互使用，可执行 `.\.venv\Scripts\Activate.ps1`，完成后用 `deactivate` 退出，无需为这些显式命令修改系统执行策略。

### Debian Linux：CPython 3.12.16 / x86_64

以下命令在上述 Debian 目标的 `backend` 目录执行，先确认 `python3.12` 实际为 **3.12.16 / x86_64**。与 Windows 一样，不覆盖已有 `.venv`，不同用途使用各自干净环境。

```bash
python3.12 --version
python3.12 -c 'import platform; print(platform.machine())'
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: --index-url https://pypi.org/simple pip==26.1.2
```

按用途只选择以下一条安装命令：

```bash
# 开发环境
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r locks/linux-py312-dev.txt
```

```bash
# 独立的纯运行环境
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r locks/linux-py312-runtime.txt
```

`--require-hashes` 校验安装文件的哈希，`--only-binary=:all:` 要求使用 wheel（可直接安装的发行文件），不回退到源码构建。**向已有开发环境安装运行锁不会自动卸载开发包**，因此不能据此证明纯运行环境可用。安装后用对应虚拟环境的 `python -m pip check` 检查依赖兼容性。

HTTPX 用于生产 GitHub 客户端，保留在运行声明；pytest、Ruff、mypy、pytest-cov、httpx2 属于开发声明。安装开发锁会包含运行依赖，但不把开发工具变成服务的运行依赖。两平台实际安装记录见下文；本批第六批未安装、升级或卸载任何包，也未创建虚拟环境。

### 锁文件维护

修改直接依赖时，先改对应范围声明，再在各自目标环境重新解析；先生成运行锁，再生成受运行锁约束的开发锁，并核对共有版本一致。保留 `uvicorn[standard]` 选中的平台依赖，使用哈希和仅 wheel 安装检查，再在全新环境实际安装、验证。不要把 Windows 锁直接用作 Linux 安装输入。

工具版本、生成约束、`pip-compile --generate-hashes --strip-extras` 和安装预演方式详见各锁文件头。文件头描述生成批次的条件，后续实际安装验收另列于下文。生成和预演不等于运行验收；维护时不在 CI 中重新生成锁，也不只修改版本而保留旧哈希。

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

在 `backend` 目录执行。Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Linux：

```bash
.venv/bin/python -m uvicorn app.main:app --reload
```

`app.main:app` 表示加载 `app/main.py` 中名为 `app` 的对象；`--reload` 用于开发时自动重新加载代码。按 `Ctrl+C` 停止服务。

启动成功后可访问：

- 健康检查：`http://127.0.0.1:8000/health`
- 可交互的接口文档：`http://127.0.0.1:8000/docs`

打开 `/docs` 不会自动请求 GitHub；在页面执行两个 GitHub 接口会触发真实上游请求。本批 Day 6 没有启动服务或执行实网请求；此前最小实网验收使用进程内接口流程，没有启动监听服务。

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

### POST /api/v1/github/file-review

接收 `GitHubFileReviewRequest`，仅允许 `repository_url`、`file_path` 两个必填字段，不接受额外字段。`repository_url` 复用上述严格字符串、1–2048 个字符及 GitHub 主页 URL 规则；`file_path` 的规则见下文。使用仓库默认分支，不提供 `ref`、`branch` 或 `commit` 参数。

以下请求与响应均为**模拟示例**，假设模拟文件没有命中审查规则；不表示实际访问过示例仓库或知道其中源码的审查结果。

```json
{
  "repository_url": "https://github.com/example-owner/example-repo",
  "file_path": "src/Foo.py"
}
```

成功时返回 HTTP 200 和 `GitHubFileReviewResponse`：

```json
{
  "repository_url": "https://github.com/example-owner/example-repo",
  "file_path": "src/Foo.py",
  "review": {
    "language": "python",
    "issue_count": 0,
    "issues": []
  }
}
```

顶层只有 `repository_url`、`file_path`、`review`。前两个字段保留请求中合法输入的原始形式，包括仓库地址的大小写和合法末尾斜杠。`review` 复用原有 `ReviewResponse`，问题字段、行号和排序语义与源码提交接口相同；不返回 `code`、`content`、完整上游 JSON 或下载链接。

处理流程为：请求校验 → 一次固定 Contents API 读取 → 校验并解码 → 将完整源码原样交给现有 `review_python` 一次 → 组织嵌套响应。空文件返回 HTTP 200、`issue_count: 0`、`issues: []`；语法错误也返回 HTTP 200，但 `review` 中包含一条 `PY_SYNTAX`，不混入其他规则的部分结果。旧 `/api/v1/reviews` 仍拒绝空源码字符串。

文件业务错误示例：假设模拟上游声明文件大小为 20001 字节，返回 HTTP 422：

```json
{"detail": "Python 文件超过大小限制，最多允许 20000 字节。"}
```

#### 文件路径规则

- `file_path` 必须是真正的字符串，字段长度为 1–512 个字符，不将整数或布尔值转成字符串。
- 仅允许 ASCII 字母、数字、点、下划线、连字符和 `/`；仅接受相对路径，拒绝空路径段及独立的 `.`、`..` 段，例如 `/api.py`、`src//api.py`、`src/../api.py`。
- 只接受小写 `.py` 后缀；路径本身不要求全部小写，所以 `src/Foo.py` 合法，`src/Foo.PY` 不合法。
- `package..py` 可以合法，连续点出现在文件名中不等于独立的 `..` 路径段。
- 拒绝反斜杠、百分号编码、空格、控制字符、中文路径、查询和片段；不自动修正或规范化输入。

#### 文件大小与编码

| 校验对象 | 上限与单位 | 适用范围 |
| --- | --- | --- |
| 文件 HTTP 原始响应体，含 JSON 和 Base64 内容 | `65_536` 字节 | 仅文件读取，流式读取时累计检查，超限即停止后续读取，不依赖 `Content-Length` |
| Base64 `content` | 仅移除 CR/LF 后最多 `26_668` 个字符 | 文件内容字段，不清理空格或 Tab |
| 解码后的 Python 源码 | 最多 `20_000` 字节 | 同时检查上游声明大小与实际解码字节数一致，允许 0 字节 |
| 原有 `/api/v1/reviews` 的 `code` | `1–20000` 个字符 | 提交字符串审查，仍要求非空，与远程文件的字节限制不同 |

`20_000` 字节不是 20 KiB；UTF-8 中文通常需要多个字节，不能把字节数当作字符数。上述文件响应体上限**不适用于仓库元数据查询**，元数据查询尚无相同的流式正文限制。

文件请求发送 `Accept-Encoding: identity`。需要读取正文时，只接受未声明 `Content-Encoding` 或其值为 `identity` 的响应，其他 HTTP 内容编码归为上游异常。JSON 正文按严格 UTF-8 解析；文件内容须为 Base64，使用严格校验解码，不补 padding（末尾填充）、不忽略非法字符，源码再按严格 UTF-8 解码，不替换解码错误。验证路径、文件名、类型和必需字段通过后才返回字符串。

### GitHub 查询与文件审查的错误响应

输入验证沿用现有自定义处理器。例如，仓库元数据查询请求体为 `{}` 时返回 HTTP 422：

```json
{
  "detail": [
    {"loc": ["body", "repository_url"], "type": "missing", "msg": "Field required"}
  ]
}
```

`loc` 表示错误位置，`type` 表示错误类别，`msg` 是说明；错误项不包含原始输入或内部校验上下文。客户端错误使用固定的 `detail` 字符串，例如仓库元数据接口的 HTTP 404：

```json
{"detail": "仓库不存在或不可公开访问。"}
```

| HTTP 状态 | 实际含义 |
| --- | --- |
| `200` | 请求成功；源码审查发现问题（包括 `PY_SYNTAX`）仍属于正常审查结果 |
| `422` | 请求体、字段类型、长度、URL 或文件路径验证失败时不发送上游请求；文件过大或不支持类型/源码编码也返回 422，但可能已读取上游响应 |
| `404` | GitHub 返回 404，仓库或文件不存在，或不可匿名公开访问；具体说明依接口而定 |
| `502` | 普通 GitHub 403、重定向、其他非预期上游状态、无效响应数据或网络请求错误 |
| `503` | GitHub 429，或有明确证据属于限流的 GitHub 403 |
| `504` | HTTPX 连接、读取、写入或连接池等待超时 |

**普通 403 不会自动被认定为限流。** 只有 `X-RateLimit-Remaining` 去除首尾空白后为 `0`，或者消息明确包含 `API rate limit` / `secondary rate limit`（忽略大小写），才将 403 归为限流。GitHub 的其他状态也不会原样透传。

限流时，HTTP 503 的响应为：

```json
{"detail": "GitHub 请求受到限流，请稍后重试。"}
```

仓库元数据接口的其余固定说明保留为 `GitHub 拒绝访问该仓库。`、`GitHub 返回了重定向，请使用仓库当前的主页地址。`、`GitHub 返回了无效或非预期的响应。`、`GitHub 网络请求失败。` 和 `GitHub 请求超时。`。路由不直接返回客户端异常字符串、上游 JSON 或请求头。

文件审查路由将九种明确客户端异常映射如下，说明文本固定，不使用底层异常字符串：

| 客户端异常 | HTTP 状态 | `detail` |
| --- | --- | --- |
| `GitHubNotFoundError` | `404` | `仓库或文件不存在或不可公开访问。` |
| `GitHubRateLimitError` | `503` | `GitHub 请求受到限流，请稍后重试。` |
| `GitHubForbiddenError` | `502` | `GitHub 拒绝访问该仓库。` |
| `GitHubRedirectError` | `502` | `GitHub 返回了重定向，请使用仓库当前的主页地址。` |
| `GitHubUpstreamError` | `502` | `GitHub 返回了无效或非预期的文件响应。` |
| `GitHubTimeoutError` | `504` | `GitHub 请求超时。` |
| `GitHubNetworkError` | `502` | `GitHub 网络请求失败。` |
| `GitHubFileTooLargeError` | `422` | `Python 文件超过大小限制，最多允许 20000 字节。` |
| `GitHubUnsupportedFileError` | `422` | `GitHub 文件类型或编码不受支持。` |

请求模型验证失败的 `detail` 为数组，每项仅含 `loc`、`type`、`msg`；文件业务限制错误的 `detail` 为固定字符串。文件响应体超限、无效 JSON/Base64、字段类型错误或声明大小不一致等仍为 502。不能笼统地把“非文件响应”都写成 422：目录列表不是 JSON 对象，实际归为 `GitHubUpstreamError`；具备必要字段但含不支持类型、显式链接或子模块标记的对象才可能归为 `GitHubUnsupportedFileError`。文件路由只转换下载阶段的九种异常，不掩盖审查器或响应组装的程序错误。

## GitHub 查询的安全与资源管理

- **校验原始字符串后再解析。** 拒绝空白、控制字符、百分号编码和反斜杠，防止解析器清理字符后产生歧义。
- **协议和域名精确匹配。** 仅接受 HTTPS 和 `github.com`，不允许用户信息、显式端口、查询参数或片段，包括空的 `?`、`#`。
- **路径采用明确的字符规则。** 只允许 `/owner/repo` 和最多一个末尾斜杠。owner 仅允许 ASCII 字母、数字及单个内部连字符；repo 仅允许 ASCII 字母、数字、点、下划线和连字符，不能是独立的 `.` 或 `..`，也不能以 `.git` 结尾（忽略大小写）。
- **固定 HTTP 请求目标。** 客户端只用已验证的 owner、repo 和文件相对路径构造 `https://api.github.com/repos/{owner}/{repo}` 或 `/repos/{owner}/{repo}/contents/{file_path}`，主机始终为 `api.github.com`。不请求用户提交的完整 URL，也不访问 `download_url` 等上游链接，不接受用户指定的 API 地址、请求头或认证信息。这用于防止 SSRF，即诱导服务器请求任意地址。
- **不跟随重定向，保持 TLS 证书验证。** 不访问上游 `Location` 指定的目标。
- **匿名读取公开信息。** 不需要配置 GitHub Token，也不发送 `Authorization`。匿名请求受到 GitHub API 限额约束，多个调用者可能共享服务器来源 IP 的额度；具体规则以 [GitHub 官方限额文档](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 为准。
- **设置明确超时。** connect 为 3 秒，read、write、pool 各为 5 秒；这些是不同网络阶段的超时，不是整个请求总耗时最多 5 秒。
- **清理连接和响应流。** 两个 GitHub 路由复用同一依赖管理方式，每次请求创建客户端并在成功或异常结束时关闭；文件读取的响应流也通过上下文管理释放。客户端支持 `with`、`close()` 和公开的 `is_closed`；模块导入时不创建长期存活的 HTTP 客户端。
- **限制错误信息。** 元数据经过响应模型验证，文件数据经过字段、大小和解码校验，失败时返回固定说明；原始错误正文和底层异常文本不会直接透传给 API 调用方。
- **校验链接标记但不作绝对保证。** 拒绝显式链接和子模块标记；Contents API 可能返回仓库内符号链接目标的普通文件内容，因此不能证明原路径一定不是链接。

URL 和文件路径校验函数本身不进行 DNS 或 HTTP 请求；仓库或文件是否存在、是否可公开访问，需要后续实际 GitHub 响应确认。Python 源码只作为 AST 分析的数据，不执行、导入或保存。

## 当前限制与不支持的功能

- **仅审查 Python。** 接受提交的源码字符串，或公开仓库默认分支中的单个受限 Python 文件；不支持其他语言、整个仓库扫描、多文件批量审查或代码 diff。可解析的 Python 语法取决于运行服务的解释器版本。
- **规则只匹配语法结构。** 不追踪函数别名、变量绑定、跨文件关系或 `builtins.eval(...)`、`obj.print(...)` 等属性调用；自定义同名函数也可能被提示。
- **不是完整的 Python 正确性检查。** `PY_SYNTAX` 表示 AST 解析失败，不代表能发现所有编译阶段或运行时错误。注释和普通字符串中的规则名称不会触发问题；字符串插值表达式中的真实调用仍会被遍历。
- **提示需要结合上下文判断。** `print()` 有时是正常输出；`PY_PRINT` 只是提示，并不自动认定代码有缺陷。
- **不提供完整安全分析或安全保证。** 当前规则不是完整的漏洞扫描，也没有沙箱执行或进程隔离。已有源码长度、远程文件字节和文件响应体大小限制，但不能替代完整的 CPU、内存、并发及总耗时资源隔离。
- **GitHub 范围受限。** 只支持公开仓库元数据查询和默认分支单个 Python 文件读取审查；不读取 PR 或 diff，不支持私有仓库、Token、webhook 或自动发布评论；GitHub Actions 仅负责验证本项目。
- **GitHub 客户端没有重试、缓存或认证。** 当前也没有调用方鉴权或应用级请求限流；文件读取有受限响应体和源码配额，元数据查询尚无相同的流式正文限制，不属于完整的生产环境防护。
- **网络配置固定。** `trust_env=False`，不会自动采用环境代理或自定义证书变量；需要代理或企业证书的环境可能无法直接连接。2026-10-30 已完成最小应用实网验收，但期间也遇到匿名限流和连接超时，不保证所有网络环境或后续请求始终可用；外部 HTTP 访问和部署验收仍未覆盖。
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

上面的健康检查和源码提交接口不需要 GitHub 网络。访问 `/docs` 也不会自动查询 GitHub；在文档页面执行仓库元数据查询或文件审查接口会发出真实 GitHub 请求。本批 Day 6 未执行这些命令；此前最小实网验收的范围和结果见下文历史记录。

## 运行自动化测试

按上文安装对应平台的开发锁后，在 `backend` 目录执行，无需另行启动服务。Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Linux 的同一命令为 `.venv/bin/python -m pytest -q`。

`pyproject.toml` 已将测试发现目录设置为 `tests`，并将 `backend` 加入测试导入路径，所以命令无需额外修改 `PYTHONPATH`。`TestClient` 在进程内调用接口，不需要真实 GitHub 或 AI 服务。

现有测试验证健康检查、四条审查规则、结果排序、同一行多个问题、无问题代码、字符串和注释、长度边界、非法请求，以及提交的源码不会被执行。审查器测试使用 pytest 的临时目录确认写文件语句没有实际创建目标文件。

GitHub 模型测试覆盖合法 URL、伪装域名、路径及字符边界、严格类型和额外字段。客户端测试通过 HTTPX `MockTransport` 返回模拟响应或主动抛出超时、连接异常；它们不会等待真实网络超时。API 测试使用 FastAPI `TestClient`，覆盖七字段响应、422 格式、各类错误映射、异常文本脱敏和旧接口回归。

Day 5 的三个文件测试模块进一步覆盖相对 Python 路径、严格模型、文件响应体和源码大小边界、Base64/UTF-8 解码、显式链接标记及流关闭。API 集成测试使用真实客户端和真实审查器，只模拟上游传输，验证一次读取、原样审查、不回显源码、空文件与语法错误，以及下载失败时不进入审查器；含顶层 `raise RuntimeError` 的源码也只作为字符串被解析。

测试阻断真实网络；依赖覆盖在结束时恢复。资源测试既检查模拟依赖，也通过替换构造器的传输层执行真实的生产依赖，验证每次请求独立创建并关闭客户端。这些测试不验证真实 GitHub 数据、DNS、TLS 握手或实际服务延迟。

新增的解析器异常测试使用 `monkeypatch` 临时模拟失败，避免使用可能耗尽资源的输入来触发异常；这不等于验证了真实资源耗尽场景。

## 在本地运行全部质量检查

先按“环境准备”安装对应平台的开发锁，然后在 `backend` 目录执行。Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy app
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q
```

Linux：

```bash
.venv/bin/python -m pip check
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy app
.venv/bin/python -m pytest --cov=app --cov-report=term-missing -q
```

这与本次 CI 配置的检查内容和顺序一致：先独立执行 `pip check` 检查依赖冲突，再执行原四项检查。CI 使用 setup-python 选出的解释器创建 `.venv`，之后安装和检查均显式使用 `.venv/bin/python`，不依赖跨步骤激活。每条命令都应成功，不能只根据最后一条的结果判断所有检查通过。

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

### Day 4 本地验收记录（历史）

以下保留此前 Day 4 的本地验收记录：2026-10-29，Windows 虚拟环境，实际解释器 Python 3.14.6。它不代表后续批次重新执行检查，统计的代码也早于 Day 5。

| 检查 | Day 4 历史结果 | 退出码 |
| --- | --- | --- |
| pytest 与覆盖率 | `335 passed in 2.40s`；`app` 共 197 条可执行语句，未覆盖 0 条，行覆盖率 100% | `0` |
| Ruff 代码检查 | `All checks passed!` | `0` |
| Ruff 格式检查 | `11 files already formatted` | `0` |
| mypy | `Success: no issues found in 6 source files` | `0` |

该次测试输出没有列出警告，使用普通覆盖率命令，没有添加可选的 `-W error`。该次检查没有安装依赖、自动修复或格式化文件，也没有执行真实 GitHub API 请求。

该次本地通过本身不能证明 Python 3.12、Linux 或 GitHub 云端通过；后续 Day 4 远程 CI 记录见下文。100% 行覆盖率不代表验证了所有输入组合或分支；当前没有性能基准。

### Day 5 本地全量验收记录（历史，提交前）

执行日期：2026-10-29（本机时区 UTC+08:00）。当时使用已有的 `backend/.venv`，实际解释器为 **Python 3.14.6**，覆盖率报告平台为 `win32`。CI 和静态检查的目标为 Python 3.12，该次本地验收没有切换解释器。这是此前记录，本批 Day 6 未重新执行。

以下命令在当时的 `backend` 目录依次执行，每项均记录了实际退出码：

| 命令 | Day 5 本地历史输出摘要 | 退出码 |
| --- | --- | --- |
| `.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing -q` | `691 passed in 7.18s`；333 条可执行语句，未覆盖 0 条，`app` 行覆盖率 100% | `0` |
| `.\.venv\Scripts\python.exe -m ruff check .` | `All checks passed!` | `0` |
| `.\.venv\Scripts\python.exe -m ruff format --check .` | `14 files already formatted` | `0` |
| `.\.venv\Scripts\python.exe -m mypy app` | `Success: no issues found in 6 source files` | `0` |

覆盖率报告的各模块记录如下；`Missing` 列全部为空，没有未覆盖行号：

| 模块 | 可执行语句 | 未覆盖 | 行覆盖率 |
| --- | --- | --- | --- |
| `app/__init__.py` | 0 | 0 | 100% |
| `app/github_client.py` | 157 | 0 | 100% |
| `app/main.py` | 65 | 0 | 100% |
| `app/reviewer.py` | 12 | 0 | 100% |
| `app/rules.py` | 23 | 0 | 100% |
| `app/schemas.py` | 76 | 0 | 100% |
| 合计 | 333 | 0 | 100% |

该次四项检查首次运行即通过，状态为 **Day 5 本地全量验收通过**，没有当次失败或修复重跑记录。pytest 无失败，输出未列出警告。当时只运行了一次全量 pytest，未另外运行不带覆盖率的全量测试。

该记录的统计口径为 `--cov=app` 的行覆盖率，没有添加 `--cov-branch`、覆盖率门槛、警告过滤、测试排除或其他改变统计口径的参数。当前配置没有最低覆盖率门槛；100% 仅说明当时测试执行了所有计入统计的应用代码行，不能证明所有分支、输入组合或安全边界都已验证。

Day 5 第四批文档与本地验收时，只更新 README，使用已有依赖完成检查。当时没有安装或升级依赖、重建环境、自动格式化或修复、启动服务、执行实网请求、暂存、提交或推送；检查完成后只改文档，没有重复全量检查。后续提交、推送、远程 CI 和最小实网验收已完成，记录如下。

### Day 5 提交与远程 CI 验收记录（历史）

以下为此前已核实的验收记录，本批 Day 6 第六批只修改文档与 CI 配置，没有重新查询远程仓库或 CI：

- 提交：`0adebe6b11a244d33ea41466d305ec7377cd2598`。
- 提交信息：`feat: review Python files from public GitHub repositories`。
- 已推送分支：`day4/github-api`。
- [Backend CI #4](https://github.com/tiezhi7/RepoPilot/actions/runs/40920455169) 对应上述完整 SHA，最终结论为 `success`。

| 项目 | Day 5 远程 CI 历史结果 |
| --- | --- |
| 环境 | Ubuntu 24.04.5，Python 3.12.15 |
| pytest | `691 passed in 2.91s` |
| `app` 行覆盖率 | 100%；347 条语句，0 条未覆盖 |
| Ruff | 通过 |
| 格式检查 | 14 个文件符合格式 |
| mypy | 6 个源文件检查通过 |

本地记录为 Python 3.14.6、333 条语句，CI 记录为 Python 3.12.15、347 条语句；两者分别保留，没有调查或推测统计差异的原因。这些历史结果不代表 Day 6 的锁定依赖与 CI 变更已经通过远程验证。

### Day 5 最小应用实网验收记录（历史）

日期：**2026-10-30，北京时间**。以下也是此前已核实记录，本批 Day 6 没有重新执行。验收通过进程内 FastAPI 接口流程，使用生产 GitHub 客户端和真实 GitHub HTTPS 请求：

| 场景 | 历史结果 |
| --- | --- |
| 健康接口 | HTTP 200 |
| 非法文件路径 | HTTP 422 |
| 读取 `psf/requests` 的 `src/requests/api.py` 并审查 | HTTP 200，`issue_count` 为 0 |
| 随机生成的不存在文件路径 | HTTP 404，固定错误说明正确 |

期间曾遇到匿名限流和连接超时，后续完成了上述成功场景；不能据此宣称网络始终稳定。验收没有启动监听服务，因此没有覆盖外部 HTTP 访问或部署验收，也不代表所有网络、代理、证书环境均已验证。

本地离线测试、远程 CI 和最小应用实网验收是三份独立记录。本地和 CI 中的自动化测试使用模拟上游响应；最小实网记录才覆盖上述真实 GitHub 上游访问场景。本批 Day 6 第六批没有重新执行这三项验收。

### Day 6 平台锁与全新环境验收记录（此前批次）

以下记录来自 2026-10-30 已完成的 Windows 第三批和 Debian Linux 第五批验收，**不是本批第六批重跑的结果**。运行环境、开发环境各自新建，未继承旧项目环境的包；安装均要求哈希和可用 wheel，并保持 TLS 校验。

| 项目 | Windows 本地历史结果 | Debian Linux 本地历史结果 |
| --- | --- | --- |
| 目标环境 | Windows AMD64 / CPython 3.14.6 | Debian 12 / x86_64 / glibc 2.36 / CPython 3.12.16 |
| 运行锁 / 开发锁 | 22 包 / 39 包 | 23 包 / 39 包 |
| 实际安装与逐包版本核对 | 两个新环境均通过 | 两个新环境均通过 |
| `pip check` | 两环境均退出 0 | 两环境均退出 0 |
| 运行环境冒烟 | 导入、Uvicorn 入口、健康接口、正常源码审查、四路由 OpenAPI 通过 | 相同检查通过，另验证 uvloop 事件循环创建与关闭 |
| 开发环境 Ruff / 格式 / mypy | 均退出 0；14 个文件符合格式、6 个源文件类型检查通过 | 均退出 0；14 个文件符合格式、6 个源文件类型检查通过 |
| 开发环境 pytest | `691 passed in 6.26s`，未报告警告 | `691 passed in 10.47s`，未报告跳过项或警告 |
| `app` 行覆盖率 | 333 条语句、0 未覆盖、100% | 347 条语句、0 未覆盖、100% |

两平台的引导包 pip 均固定为 26.1.2，单独记录，不计入锁定包数量；验收环境没有安装 pip-tools。运行锁全部包含于同平台开发锁且共有版本一致，纯运行环境没有开发独有包。`uvicorn[standard]` 的条件依赖已按包元数据核实完整；Linux 需要 `uvloop`，Windows 开发依赖中的 `colorama` 不进入 Linux 开发锁。

两平台的运行环境冒烟各保留一条 `StarletteDeprecationWarning`：`Using httpx with starlette.testclient is deprecated; install httpx2 instead.` 均未过滤，也未因此向运行环境增加 httpx2；两次开发环境 pytest 均未报告警告。事件循环和 TestClient 已关闭。

Debian 使用上文完整镜像摘要；实际 Python 为 `3.12.16 (main, Oct 28 2026, 02:01:35) [GCC 12.2.0]`。安装后的检查阶段已断开本批容器网络，缓存、日志、覆盖率和临时测试文件均放在专用临时目录，项目只读挂载；两个验收容器已清理。

上述覆盖率均为行覆盖率，未收集分支覆盖率；不推测两平台语句数差异的原因。运行冒烟通过进程内 TestClient 执行，没有启动监听端口，也没有调用 GitHub 业务接口。它们证明所述新环境中的安装与最小应用行为，不能替代 Ubuntu CI、外部 HTTP 访问或部署验收。

本批第六批只调整 README 和 CI 配置，并做 YAML 与关键结构静态检查，未重跑以上安装、冒烟、pytest、Ruff 或 mypy。**Ubuntu 24.04 / Python 3.12.16 是本次准备的 CI 目标，Day 6 远程 CI 尚未执行或验证。**

## Ruff 代码质量检查与格式检查

先安装对应平台的开发锁。在 `backend` 目录运行代码质量检查，以下为 Windows 命令，Linux 将解释器路径换为 `.venv/bin/python`：

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

当前未设置分支或路径过滤，保留 `backend` job（显示名称 **Backend checks**）和只读权限。工作流固定 `runs-on: ubuntu-24.04`，使用原有 `actions/checkout@v7` 和 `actions/setup-python@v7`；checkout 配置 `persist-credentials: false`，setup-python 指定 `python-version: "3.12.16"`、`architecture: "x64"`。

所有 `run` 步骤的工作目录仍为 `backend`。先使用 setup-python 提供的 `python` 创建新虚拟环境；随后均显式使用 `.venv/bin/python`：

```bash
python -m venv .venv

# 以下版本与安装命令位于新虚拟环境中
set -euo pipefail
.venv/bin/python --version
.venv/bin/python -m pip install --only-binary=:all: --index-url https://pypi.org/simple pip==26.1.2
.venv/bin/python -m pip --version
.venv/bin/python -m pip install --require-hashes --only-binary=:all: --index-url https://pypi.org/simple -r locks/linux-py312-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy app
.venv/bin/python -m pytest --cov=app --cov-report=term-missing -q
```

上面按执行顺序展示命令；工作流将创建环境、固定 pip 和输出版本、安装开发锁、`pip check`、四项质量检查分为独立步骤。`pip check` 检查已安装包之间的依赖关系，不替代应用测试。项目依赖不再从范围声明安装，CI 不安装 pip-tools，也不重新生成锁文件。

任务仅申请 `permissions: contents: read`，无需手动配置密钥。没有增加矩阵、容器 job、缓存、部署或发布；任意安装、哈希校验或检查失败都会使 job 失败，未设置忽略失败。覆盖率仍只测量行覆盖率，没有最低覆盖率门槛。

**此前 Day 4 远程验收记录：** 提交 `d584a95d48281ec19fa3429f90378366f2081c88` 已推送，Backend CI 成功，见[历史运行记录](https://github.com/tiezhi7/RepoPilot/actions/runs/40859087524)。该链接只代表 Day 4；Day 5 的成功运行是上文另列的 Backend CI #4。**本批 Day 6 未访问或重新查询这些运行。** CI 成功本身不能替代应用实网验收，后者按上文记录的场景单独完成。

Day 5 提交已经由上述运行验证；Day 6 的依赖声明、四份锁、CI 和文档调整尚未提交，不在这些历史运行的验证范围内。**CI 配置已改用固定依赖，但 Day 6 的 Ubuntu 远程 CI 尚未执行或验证。** 本地 YAML 语法和结构检查不能替代 GitHub 实际执行，也不能用 Debian 通过推断 Ubuntu 通过。

后续经授权提交并推送新变更后，可在仓库页面选择 **Actions → Backend CI → 对应运行记录 → Backend checks**，核对完整提交 SHA，再展开环境版本、严格安装、`pip check` 和四项检查的实际日志。参见 [GitHub 查看工作流运行历史](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-workflow-run-history)。

## 下一阶段计划（尚未实施）

Day 5 的提交推送、远程 CI 和最小应用实网验收已有历史记录；Day 6 已完成依赖分离、四份锁及 Windows / Debian 全新环境验收，本批另完成 CI 配置与文档同步。后续工作按独立范围进行：

1. 完整审阅累计八项变更及四份未跟踪锁文件的实际内容，确认范围和兼容性。
2. 经授权创建本地提交、推送指定分支；本批不执行这些 Git 操作。
3. 核对对应 Day 6 完整提交 SHA 的 Ubuntu 24.04 / Python 3.12.16 CI 日志，记录真实安装、哈希、`pip check` 及四项检查结果，不预填测试数量或覆盖率。
4. 单独安排监听服务及外部 HTTP 访问验收；进程内 TestClient 成功不代表监听服务已验证。
5. 单独安排部署验收，并在需要时另行授权复验新依赖环境中的 GitHub 实网访问；Day 5 的最小实网成功记录继续保留，不能由本次离线结果推导新环境的网络表现。

应用级限流、缓存和覆盖率门槛也需要后续单独评估；本批未添加。

GitHub PR/Diff 集成、整仓库扫描、AI 审查、私有仓库与 Token、评论发布、前端和数据库仍属于后续需要单独设计与确认的工作，不在本批实施。
