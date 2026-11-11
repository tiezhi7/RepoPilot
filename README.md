# RepoPilot

RepoPilot 当前是一个基于 FastAPI 的 Python 后端，支持健康检查、提交 Python 源码审查、GitHub 公开仓库元数据查询、公开仓库中单个 Python 文件的静态审查，以及公开 PR 的只读概览。PR（Pull Request，合并请求）用于提议将一组提交合入目标分支；当前概览仅返回其身份、状态、分支、提交 SHA 和变更数量，不读取修改文件、不审查 diff、不判断可合并性，也不发布评论。源码审查使用 Python 标准库 AST（抽象语法树，即代码的结构化表示），GitHub 查询和文件读取使用 HTTPX。

项目的长期目标是发展为面向 GitHub 的 AI 代码审查助手。当前尚未接入 AI；调用文件审查接口时，只读取默认分支中指定的一个受限 Python 文件，不扫描整个仓库。源码只作为 AST 分析的数据，不执行、导入或保存。

仓库已提供 GitHub Actions 持续集成配置。此前已核实：Day 5 提交已推送，Backend CI #4 成功，并在 2026-10-30 完成最小应用实网验收，详见下文历史记录。Day 6 已分离依赖声明、生成四份平台锁、完成 Windows 与 Debian Linux 全新环境验收，相关变更已提交并推送，Ubuntu 开发环境的 Backend CI #5 也已成功。Day 7 第一批在全新 Windows 运行环境中完成本机 HTTP 监听验收；Day 8 第一、第二批进一步完成 Docker 本地构建和容器端口映射链路验收，均覆盖健康检查、文档和源码审查。Day 9 新增独立 Docker checks job 和标准库真实 HTTP 冒烟脚本，保留原有 Backend checks；相关变更已提交并推送，两个 job 已在提交 `130d64ff90caf7dac2e7e78f4731f07f6d757315` 的 Backend CI #9 中通过。

Day 10 已实现 PR 请求/响应模型、受限客户端、同步路由及离线测试，将业务路由扩展为五个，冒烟脚本扩展为十项。此前已完成两平台锁定开发环境全量检查和本地运行镜像验收，随后修复 PR 链接的 ASCII 身份核对并完成定向回归。**Day 10 已随提交 `9ca16fe01532966fd1ce3dc44639b9272ca49c53` 推送，Backend CI #11 的 Backend checks 和 Docker checks 均成功。** 修复前的本地记录、修复后的定向回归与远程结果分别绑定下文的版本和环境。

**本批仅依据已有的已核实记录同步 README，并执行静态检查，没有重新安装、测试、构建、启动容器、发送 HTTP 或查询 CI。** Day 10 第八批当时也只更新文档，其整理的修复前本地验收基于 `27d0bdc4842c37a2bf735ad629ca77016667ff89` 加当时八项未提交改动，该旧 HEAD 本身不包含 Day 10 功能。Day 4—Day 9 的历史提交绑定、数字、失败和警告分别保留。PR 非法编号的 422 不代表有效 PR 实网查询通过。

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
- 第六批将后端 CI 配置为 Ubuntu 24.04 / Python 3.12.16，在独立虚拟环境中严格安装 Linux 开发锁，再执行依赖兼容性和原四项质量检查。其后相关变更已提交、推送，并由 Backend CI #5 验证指定提交的 39 包开发环境；Ubuntu 纯运行环境尚未单独验证。

### Day 7：Windows 本机 HTTP 监听验收

- 使用 Windows 运行锁安装全新 CPython 3.14.6 运行环境，核对 22 个包版本并通过 `pip check`。
- 启动独立、单进程 Uvicorn，仅监听 `127.0.0.1`，由操作系统分配端口；普通 HTTPX 客户端经真实 TCP 完成九项响应断言。
- 验证健康、Swagger UI HTML、四个业务路由的 OpenAPI 声明和源码审查；未调用两个 GitHub 业务接口。
- 第一批已关闭客户端、停止该批服务并确认原监听释放；首次临时脚本 PID 核对失败及修正经过保留在下文记录中。
- 第二批仅整理启动说明和既有验收记录，没有增加业务功能或重跑验收。

### Day 8：Docker 运行镜像与容器 HTTP 验收

- 新增 `backend/Dockerfile` 和允许列表形式的 `backend/.dockerignore`，以 `backend` 为构建上下文，固定基础镜像和 Linux 运行锁。
- 镜像使用 `/opt/venv`、非 root 用户 `10001:10001` 和复制进镜像的应用源码，不含开发专用依赖；主机代码修改后需重新构建。
- 第一批完成实际构建上下文、镜像内容及依赖检查；保留下载超时、最小参数调整和非 root pip 缓存警告记录。
- 第二批通过 Windows 宿主机动态端口完成九项真实容器 HTTP 断言，验证日志、SIGTERM 停止、状态、端口释放和本批容器删除。
- 第三批当时仅同步 README，未重跑构建或验收。验收时两个 Docker 配置尚未提交，当时的 CI 只检查 Python 依赖与代码质量，没有 Docker 构建或容器 HTTP 步骤；两个配置后来随 Day 8 提交纳入仓库。

### Day 9：Docker CI 配置、本地流程与远程 CI 验收

- `.github/workflows/backend-ci.yml` 保留原 Backend checks，新增独立 Docker checks；两个 job 没有 `needs` 依赖。
- 当时新增 `backend/scripts/smoke_http.py`，仅用 Python 标准库请求固定的 `127.0.0.1` 端口，验证九项 HTTP 行为；不导入应用或使用模拟传输，当时不调用 GitHub 业务接口。当前 Day 10 版本另增加了非法 PR 编号检查。
- Docker job 从 `backend` 构建，按实际镜像 ID 启动默认服务，核对容器归属、配置及动态端口，再运行冒烟脚本；由有时限的 `always()` 步骤收集日志、停止和普通删除本批容器。
- 已有本地记录包括一次缓存构建、Entrypoint 键缺失失败及单行修复、九项真实 HTTP 检查、退出码 23 的普通失败收尾，以及 HTTP 500 反例。
- 原反例收尾的端口探测超时，验收器最终退出 1；后续独立补查仅确认补查时无监听，不改写原退出码或推断超时原因。详细顺序和结果见下文 Day 9 记录。
- 工作流、脚本和前一批 README 已随提交 `130d64ff90caf7dac2e7e78f4731f07f6d757315` 推送至 `day4/github-api`，Backend CI #9 的 Backend checks 和 Docker checks 均成功；本地与远程的环境和结果分别记录，不混写。本批只依据已核实结果整理文档。

### Day 10：公开 PR 只读概览与十项冒烟

- 新增严格的 `GitHubPullRequestRequest`、`GitHubPullRequestResponse` 和纯 PR 编号验证函数，复用仓库主页 URL 规则，不改旧模型。
- 客户端新增 `fetch_pull_request`：输入验证后，仅一次 GET 固定的 GitHub PR API；有界读取原始正文，验证编码、字段和 PR 身份，显式映射十四个字段，不执行 AST 审查。
- 新增同步 `POST /api/v1/github/pull-request`，复用每次请求独立创建、关闭客户端的依赖，只转换七类明确查询异常；原四个业务路由保持兼容。
- 三份 PR 测试覆盖模型、客户端及 API；新增 `test_smoke_http.py`，通过临时回环 HTTP 服务和真实脚本子进程验证冒烟成功与失败，不导入应用或访问 GitHub。
- 当前冒烟检查五个业务路由，并新增 PR 编号 `0` 返回 422 且错误位置精确为 `["body","pull_number"]` 的检查，共十项；不执行有效上游 GitHub 查询。
- Windows / CPython 3.14.6 与 Debian / CPython 3.12.16 的 1309 项全量验收及本地运行镜像属于 ASCII 修复前记录。修复后完成定向回归，并随上述提交推送，由 Backend CI #11 验证两个 job，详见下文。

以上为已有功能、配置与已完成的验收；后面的“下一阶段计划”尚未实施。

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
    ├── Dockerfile
    ├── .dockerignore
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
    ├── scripts/
    │   └── smoke_http.py
    └── tests/
        ├── test_api.py
        ├── test_github_schemas.py
        ├── test_github_client.py
        ├── test_github_api.py
        ├── test_github_file_schemas.py
        ├── test_github_file_client.py
        ├── test_github_file_api.py
        ├── test_github_pull_request_schemas.py
        ├── test_github_pull_request_client.py
        ├── test_github_pull_request_api.py
        ├── test_smoke_http.py
        └── test_reviewer.py
```

- `AGENTS.md`：记录项目范围和编程协作规则。
- `.gitignore`：告诉 Git 哪些文件不需要纳入版本管理。
- `README.md`：介绍项目、目录结构及使用方式。
- `.github/workflows/backend-ci.yml`：定义独立的 Backend checks 和 Docker checks，Day 9 与 Day 10 的历史结果分别绑定 CI #9 和 CI #11 的完整提交 SHA。后续提交运行其自身版本的脚本并单独验收。
- `backend/Dockerfile`：定义固定 Linux 基础镜像、严格运行锁安装、非 root 用户和默认 Uvicorn 启动命令。
- `backend/.dockerignore`：以允许列表限制 `backend` 构建上下文，排除虚拟环境、测试、缓存及其他无关输入。
- `backend/requirements.txt`：维护 FastAPI、Uvicorn 和 HTTPX 三项运行直接依赖的版本范围，是生成运行锁的输入。
- `backend/requirements-dev.txt`：通过 `-r requirements.txt` 引入运行声明，再维护 pytest、Ruff、mypy、pytest-cov 和测试客户端 httpx2 的版本范围，是生成开发锁的输入。
- `backend/locks/windows-py314-runtime.txt`、`windows-py314-dev.txt`：Windows AMD64 / CPython 3.14.6 的完整运行、开发依赖及哈希。
- `backend/locks/linux-py312-runtime.txt`、`linux-py312-dev.txt`：Debian 12 / x86_64 / CPython 3.12.16 的完整运行、开发依赖及哈希；其中开发锁也已作为 Backend CI #5 的 Ubuntu 安装入口通过验收。
- `backend/pyproject.toml`：保存 Ruff、pytest 和 mypy 配置；覆盖率通过 pytest 命令行参数启用。
- `backend/app/__init__.py`：将 `app` 目录标识为 Python 包，便于导入其中的模块。
- `backend/app/main.py`：FastAPI 应用入口，负责接收请求和组织响应。
- `backend/app/schemas.py`：请求和响应模型、仓库 URL、相对 Python 文件路径和严格 PR 编号验证。
- `backend/app/github_client.py`：GitHub 同步 HTTP 客户端、元数据七字段映射、受限文件读取与解码、受限 PR 概览及十四字段映射、客户端异常和连接生命周期管理。
- `backend/app/reviewer.py`：解析源码、处理解析异常、调用规则收集器，并对结果排序。
- `backend/app/rules.py`：三个独立节点规则及只遍历一次 AST 的收集器。
- `backend/scripts/smoke_http.py`：仅用标准库从宿主机请求本机监听端口，等待就绪并检查十项 HTTP 行为，包含五路由 OpenAPI 和非法 PR 编号验证；通过显式条件和异常报告失败，不依赖可被 `python -O` 禁用的 `assert`。
- `backend/tests/test_api.py`：验证接口、审查规则、输入边界及源码不被执行。
- `backend/tests/test_github_schemas.py`：验证仓库 URL 安全规则及请求、响应模型。
- `backend/tests/test_github_client.py`：使用模拟响应验证请求目标、超时配置、上游错误、数据校验及连接关闭。
- `backend/tests/test_github_api.py`：验证仓库元数据路由、错误映射、依赖覆盖清理、实际依赖生命周期及原有接口兼容性。
- `backend/tests/test_github_file_schemas.py`：验证文件路径字符、长度、原始值保留，以及文件审查请求和嵌套响应模型。
- `backend/tests/test_github_file_client.py`：使用模拟传输和可追踪字节流验证文件读取、大小边界、Base64/UTF-8 解码、错误分类及流关闭。
- `backend/tests/test_github_file_api.py`：使用真实客户端和审查器搭配模拟上游，验证文件路由衔接、输入验证、错误映射、源码不执行、资源释放及旧接口回归。
- `backend/tests/test_github_pull_request_schemas.py`：离线验证严格编号、URL 原值保留、十四字段类型与必填约束、Unicode、SHA 和序列化。
- `backend/tests/test_github_pull_request_client.py`：用 MockTransport 和可追踪响应流验证单次固定请求、身份映射、1 MiB 边界、状态分类、编码和资源释放。
- `backend/tests/test_github_pull_request_api.py`：用真实应用、客户端及模拟上游验证 PR 路由、七类固定错误、422 脱敏、非预期异常、生产依赖生命周期和五路由 OpenAPI。
- `backend/tests/test_smoke_http.py`：启动临时标准库回环服务及真实脚本子进程，记录十项实际请求，验证缺路由、错误方法、错误状态/位置及 `python -O` 失败反例，并检查线程和端口收尾。
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
| `locks/linux-py312-dev.txt` | 同一 Debian 目标，包含运行依赖和开发工具，39 个锁定包；同锁已通过 Backend CI #5 的 Ubuntu 24.04.5 / Python 3.12.16 x64 开发环境验收 |

Linux 锁在实际 Debian 容器中生成并验证。基础镜像完整摘要为：

```text
python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
```

Ruff 和 mypy 的目标版本仍是 Python 3.12。静态检查配置不会自动切换解释器，也不代表任意 Python 版本或 Linux 发行版均已验证。安装前先确认解释器版本、架构与所选锁匹配。Ubuntu 24.04 / x64 / Python 3.12.16 开发环境已有独立的 Backend CI #5 结果，并非由 Debian 验收推导；该结果不覆盖 Ubuntu 纯运行锁的独立新环境。

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

HTTPX 用于生产 GitHub 客户端，保留在运行声明；pytest、Ruff、mypy、pytest-cov、httpx2 属于开发声明。安装开发锁会包含运行依赖，但不把开发工具变成服务的运行依赖。各环境实际安装记录见下文；本批文档同步未安装、升级或卸载任何包，也未创建虚拟环境。

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

打开 `/docs` 不会自动请求 GitHub；在页面提交合法的仓库元数据、文件审查或 PR 概览请求会触发真实上游请求。非法输入在验证阶段拒绝，例如 PR 编号 `0` 返回 422，不调用 PR 查询方法；这不表示框架一定不会先构造依赖客户端。上面是保留的开发启动说明，本批没有执行。Day 5 最小实网验收采用进程内接口流程，当时没有启动监听服务；后续本机及容器 HTTP 验收分别记录于下文。

### Windows 本地监听验收：手动复用示例

以下仅为手动启动参考，没有在本批执行，也不是第一批自动验收脚本的逐字重现。先使用版本已核实的 **Windows AMD64 / CPython 3.14.6** 创建独立新运行环境，按“环境准备”从 `locks/windows-py314-runtime.txt` 严格安装，固定 pip 26.1.2，并完成逐包核对和 `pip check`。已有开发环境不能代替纯运行环境。

把下面三个示例路径替换为实际存在的绝对路径：`$runtimePython` 指向新运行环境的解释器，`$backendDir` 指向真实项目的 `backend`，`$listenWorkDir` 是专用临时工作目录。不要依赖默认 `python`，也不要覆盖旧 `.venv`。

```powershell
# 以下均为路径占位示例，须先替换，不能直接照搬运行。
$runtimePython = 'C:\path\to\new-runtime\Scripts\python.exe'
$backendDir = 'C:\path\to\RepoPilot\backend'
$listenWorkDir = 'C:\path\to\http-check-workdir'

& $runtimePython -B -c "import sys, platform; print(sys.version); print(platform.machine())"
# 确认输出为 CPython 3.14.6 / AMD64 后再启动。
Set-Location -LiteralPath $listenWorkDir
& $runtimePython -B -m uvicorn app.main:app --app-dir $backendDir --host 127.0.0.1 --port 0 --workers 1
```

`-B` 避免生成 Python 字节码缓存；`--app-dir` 让 Uvicorn 从指定应用目录加载 `app.main:app`。此方式不启用 reload，使用一个服务进程。`--port 0` 由操作系统分配空闲端口，必须从 `Uvicorn running on http://127.0.0.1:<实际端口>` 日志读取，不能沿用某次历史端口。

保持启动窗口运行，可在另一个 PowerShell 窗口核对当前监听信息；下面也只是手动检查示例，本批未执行：

```powershell
$actualPort = [int](Read-Host '输入本次启动日志中的实际端口')
Get-NetTCPConnection -State Listen -LocalPort $actualPort |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

确认地址为 `127.0.0.1`，并将 `OwningProcess` 与 Uvicorn 日志中的实际服务 PID 对照，再核对其进程路径及父子关系，确认属于本次启动。Windows 虚拟环境的启动器 PID 与实际监听 PID 可能不同，不能只比较启动器 PID。

手动前台启动可先尝试 `Ctrl+C` 关闭；这是操作说明，不是下文自动验收实际使用的停止方式。停止后仍需确认本次启动器和实际服务进程已退出，原监听不再由它们占用。必要时仅处理已确认归属本次启动的 PID，不按 Python/Uvicorn 名称批量结束进程，也不要求修改执行策略或防火墙。

旧项目 `.venv` 曾出现启动退出 `103`，原因仍未定位。Day 7 验收使用全新环境，没有通过修改配置修复旧环境；本示例也不要求使用或修复它。

## 使用 Docker 运行后端（PowerShell）

以下是可复用的手动示例，**本批只审阅文档，没有执行这些命令**。先确认 Docker Desktop 的 `desktop-linux` 引擎可用，在同一个 PowerShell 窗口按顺序操作，并替换路径占位符。镜像仅在本地构建和使用，本项目没有发布可供公共拉取的应用镜像。

构建上下文是 Docker 可以读取的输入目录，**必须使用 `backend`，不能使用仓库根目录**。Dockerfile 采用“环境准备”中记录的固定基础镜像摘要，目标为 Debian 12 / Linux AMD64 / CPython 3.12.16。它创建 `/opt/venv`，固定 pip 26.1.2，从官方 PyPI 以哈希校验、仅 wheel 的方式安装 `locks/linux-py312-runtime.txt` 并执行 `pip check`；不安装开发锁或 pip-tools。

`.dockerignore` 使用允许列表，只放行 Dockerfile、.dockerignore、Linux 运行锁和 `app` 中的 Python 源码及必要包目录，排除虚拟环境、测试、缓存、字节码、.env、Git 数据及其他锁文件。镜像只复制 `app` 源码，工作目录为 `/app`，运行用户为 `10001:10001`；依赖和服务均显式使用 `/opt/venv/bin/python`。**主机源码不会自动同步进镜像；修改后需要重新构建。**

### 构建本地镜像

```powershell
$repoDir = 'C:\path\to\RepoPilot'  # 替换为真实项目绝对路径
$backendDir = Join-Path $repoDir 'backend'
$imageTag = 'repopilot-local:day8-' + [guid]::NewGuid().ToString('N')
$containerName = 'repopilot-http-' + [guid]::NewGuid().ToString('N')

docker --context desktop-linux buildx build --platform linux/amd64 --load --progress plain `
    --tag $imageTag --file (Join-Path $backendDir 'Dockerfile') $backendDir
if ($LASTEXITCODE -ne 0) { throw '构建失败，先检查日志，不继续启动容器。' }

$imageId = docker --context desktop-linux image inspect --format '{{.Id}}' $imageTag
if ($LASTEXITCODE -ne 0) { throw '无法读取已构建的本地镜像。' }
$imageId = $imageId.Trim()
```

`--platform linux/amd64` 指定目标平台，`--load` 将 buildx 构建结果加载到本地镜像存储。示例使用唯一标签，避免覆盖其他镜像；`$imageId` 是本机镜像标识，不是可从公共仓库直接拉取的镜像地址。Dockerfile 两处安装命令的 `--timeout 60 --retries 2` 用于下载等待和内部重试，不是整个构建最多耗时 60 秒；保持官方 PyPI 和 TLS 校验。

### 启动默认服务并读取动态端口

```powershell
$containerId = docker --context desktop-linux run --pull=never --detach `
    --name $containerName --network bridge --publish 127.0.0.1::8000/tcp $imageId
if ($LASTEXITCODE -ne 0) { throw '启动失败，先按本次唯一名称核查容器和日志，不重复启动。' }
$containerId = $containerId.Trim()
if ($containerId -notmatch '^[a-f0-9]{64}$') { throw '未获得明确的本批容器 ID。' }

docker --context desktop-linux port $containerId 8000/tcp
$portJson = docker --context desktop-linux container inspect `
    --format '{{json .NetworkSettings.Ports}}' $containerId
if ($LASTEXITCODE -ne 0) { throw '端口核对失败，保留本批容器 ID 以便收集日志和清理。' }
$portMap = $portJson | ConvertFrom-Json
$bindings = @($portMap.'8000/tcp')
if ($bindings.Count -ne 1 -or $bindings[0].HostIp -ne '127.0.0.1') {
    throw '端口映射不符合预期，先收集日志并停止本批容器。'
}
$actualPort = [int]$bindings[0].HostPort
$baseUrl = 'http://127.0.0.1:' + $actualPort
$baseUrl
docker --context desktop-linux logs --timestamps $containerId
```

没有覆盖镜像的用户、工作目录或 CMD，也没有挂载项目。默认命令使用 `/opt/venv/bin/python -B -m uvicorn app.main:app`，在容器内监听 `0.0.0.0:8000`，仅一个 worker，不启用 reload。宿主机只发布到 `127.0.0.1`，空的宿主机端口部分由 Docker 动态分配；必须读取本次映射，不能固定使用历史端口。

`EXPOSE 8000` 只是镜像中的端口声明，**不等于发布宿主机端口**；实际发布由 `--publish` 决定。容器 ID 也不是进程 PID，容器内 PID 与 Docker Linux VM、Windows 客户端的 PID 不能混用。

### 请求健康接口

端口已分配不代表应用已就绪。先在日志中确认 `Application startup complete` 和 Uvicorn 监听信息；手动等待应有上限，例如 45 秒，超时先保存日志再停止本批容器。下面使用普通 .NET HTTP 客户端，只请求宿主机动态端口，不加载浏览器或外部 CDN：

```powershell
Add-Type -AssemblyName System.Net.Http
$handler = [System.Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$handler.AllowAutoRedirect = $false
$client = [System.Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(5)
$response = $null
try {
    $response = $client.GetAsync("$baseUrl/health").GetAwaiter().GetResult()
    $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
    if ([int]$response.StatusCode -ne 200 -or $body.status -ne 'ok') {
        throw '健康接口响应不符合预期。'
    }
    $body
} finally {
    if ($null -ne $response) { $response.Dispose() }
    $client.Dispose()
}
```

客户端明确设置超时、绕过代理并禁止自动重定向。请求失败时也会关闭客户端；仍需执行下面的容器日志与停止清理步骤。这个健康检查示例不调用 GitHub 业务路由。

### 执行当前十项 HTTP 冒烟（手动示例）

在本次容器仍运行时，可使用版本已核实的主机 Python 绝对路径，从真实 `backend` 目录运行仓库原版脚本。以下命令只作说明，本批没有执行；不加载外部 CDN，不提交有效 GitHub 查询：

```powershell
$hostPython = 'C:\path\to\verified-python\python.exe'  # 替换为已核实的主机解释器
Set-Location -LiteralPath $backendDir
& $hostPython -B scripts/smoke_http.py --port $actualPort
if ($LASTEXITCODE -ne 0) { throw '冒烟失败，保存输出后继续收集日志并清理本批容器。' }
```

当前十项为：初始健康、Swagger HTML、五路由 OpenAPI、正常源码、print、语法错误、空 code、源码不执行、非法 PR 编号、最后健康。新增请求只向本机 PR 路由提交 `{"repository_url":"https://github.com/example/repo","pull_number":0}`，要求 422、非空错误数组及精确字段位置；不会调用 `fetch_pull_request` 或上游 GitHub。脚本仅依赖标准库，不能把其模拟服务测试通过写成真实应用或容器已通过；真实容器记录另列于 Day 10 验收节。

### 保存日志、停止并核对后删除

把 `$dockerLogDir` 换为已准备好的本次专用可写目录。以下操作只针对前面记录的完整 `$containerId`，不要按名称前缀批量停止，也不执行全局 prune：

```powershell
$dockerLogDir = 'C:\path\to\new-docker-check-logs'  # 替换为本次日志目录

docker --context desktop-linux logs --timestamps $containerId 2>&1 |
    Tee-Object -FilePath (Join-Path $dockerLogDir 'service-before-stop.log')
docker --context desktop-linux container inspect $containerId |
    Set-Content -LiteralPath (Join-Path $dockerLogDir 'state-before-stop.json') -Encoding UTF8

docker --context desktop-linux stop -t 10 $containerId
$stopCommandExit = $LASTEXITCODE
"docker stop 退出码：$stopCommandExit"

$stoppedJson = docker --context desktop-linux container inspect $containerId
if ($LASTEXITCODE -ne 0) { throw '无法读取停止状态，先保留容器并排查。' }
$stoppedJson | Set-Content -LiteralPath (Join-Path $dockerLogDir 'state-after-stop.json') -Encoding UTF8
$stopped = @($stoppedJson | ConvertFrom-Json)[0]
$stopped.State | Format-List Running, Status, ExitCode, OOMKilled, StartedAt, FinishedAt
docker --context desktop-linux logs --timestamps $containerId 2>&1 |
    Tee-Object -FilePath (Join-Path $dockerLogDir 'service-after-stop.log')
```

分别核对 `docker stop` 命令退出码和 `State.ExitCode`，不能混为一谈，也不能只凭某个退出码断言正常关闭。确认 `Running=false`、`Status=exited`、没有 OOM，日志含 `Application shutdown complete` 和 `Finished server process`，并确认原宿主机端口不再提供该服务；必要时按此容器 ID 查询有时间边界的 Docker 事件，区分 SIGTERM 与 SIGKILL。若异常或日志缺失，保留记录并排查，不把强制清理写成正常停止通过。

确认状态和日志后，再单独删除这个已停止的容器：

```powershell
if ($stopCommandExit -ne 0 -or $stopped.State.Running -or $stopped.State.Status -ne 'exited') {
    throw '停止未确认，不执行删除。'
}
docker --context desktop-linux container rm $containerId
if ($LASTEXITCODE -ne 0) { throw '本批容器删除失败，先检查实际状态。' }
```

保留本地镜像供后续使用，不删除其他容器、镜像、卷或构建缓存。以上是手动使用步骤，不是自动验收脚本的逐字重现；已有构建、HTTP 与停止结果分别列于 Day 8 和 Day 10 验收记录。

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

### POST /api/v1/github/pull-request

读取公开仓库的一个 PR（合并请求）的只读概览，使用 `GitHubPullRequestRequest`。请求只有两个必填字段，禁止 `token`、`api_url`、`ref`、`branch` 等额外字段：

- `repository_url`：真正的字符串，长度 1–2048 个字符，复用仓库主页 URL 规则；不接受完整 PR URL，保留合法原字符串的大小写和末尾斜杠。
- `pull_number`：真正的整数，范围 **1–2147483647**；在类型转换前验证，拒绝 `true`/`false`、数字字符串和浮点数，不自动转换或清理。这个上限是项目策略，不是 GitHub 官方编号上限。

以下请求与完整成功响应均为**模拟示例**，不表示实际访问过示例仓库；示例 SHA 仅为符合格式的模拟值，不指称真实提交。

```json
{
  "repository_url": "https://github.com/example-owner/example-repo",
  "pull_number": 42
}
```

成功返回 HTTP 200 和 `GitHubPullRequestResponse`：

```json
{
  "repository_url": "https://github.com/example-owner/example-repo",
  "pull_number": 42,
  "title": "修复重试逻辑",
  "state": "open",
  "draft": false,
  "merged": false,
  "html_url": "https://github.com/example-owner/example-repo/pull/42",
  "base_ref": "主线/发布",
  "head_ref": "功能/重试",
  "base_sha": "0123456789abcdef0123456789abcdef01234567",
  "head_sha": "89abcdef0123456789abcdef0123456789abcdef",
  "changed_files": 2,
  "additions": 10,
  "deletions": 3
}
```

响应严格限定为下面 **14 个必填平铺字段**，没有默认值，不接受 `null` 或额外字段；不返回完整 `base`/`head` 对象、正文 `body`、diff/patch 链接、源码或审查结果。

| 字段 | 类型及约束 | 含义 |
| --- | --- | --- |
| `repository_url` | 字符串，复用仓库 URL 校验 | 请求的合法原值 |
| `pull_number` | 严格整数，同请求范围 | 上游 `number`，须与请求编号一致 |
| `title` | 非空字符串 | 标题，允许中文和 emoji |
| `state` | 仅 `open` / `closed` | PR 状态，不增加 `merged` 枚举 |
| `draft` | 严格布尔值 | 是否为草稿 |
| `merged` | 严格布尔值 | 是否已合并，不根据其他字段推导 |
| `html_url` | 非空字符串，客户端另要求 ASCII 并核对身份 | 通过核对的原 PR 网页地址，不访问该链接 |
| `base_ref` | 非空字符串 | 目标分支名称，允许斜杠和 Unicode |
| `head_ref` | 非空字符串 | 来源分支名称，允许斜杠和 Unicode |
| `base_sha` | 恰好 40 位小写十六进制字符串 | 查询时返回的目标端提交 SHA |
| `head_sha` | 恰好 40 位小写十六进制字符串 | 查询时返回的来源端提交 SHA |
| `changed_files` | 严格非负整数 | 修改文件数量 |
| `additions` | 严格非负整数 | 新增行数 |
| `deletions` | 严格非负整数 | 删除行数 |

严格布尔值不接受 `0`、`1` 或字符串；三项计数不接受布尔值、浮点数或数字字符串。标题、分支等字符串不做 strip 或类型转换；分支名称不套用 Python 文件路径规则。SHA 不接受短值、大写、空白或尾部换行，也不自动改写大小写。两端 SHA 是查询时的提交信息，**不是合并结果，也不表示 PR 此后不会变化**；不使用 `merge_commit_sha` 代替它们。关闭不等于已合并，客户端不自动纠正 `state`、`draft`、`merged` 的组合。

#### 单次请求、正文限制与身份核对

客户端在任何网络调用前复用仓库 URL 和严格编号验证，只用验证后的 owner、repo、整数编号发送一次固定 GET：

```text
https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}
```

不先查询仓库元数据，不读取 PR 文件列表、diff、patch 或评论，不访问响应中的任何链接，不轮询 `mergeable`，不发送写操作或认证信息。PR 概览不调用 `review_python` 或其他 AST 审查逻辑；元数据中的代码样式文本仍是普通字符串。

- PR 原始 HTTP 正文最多 **1,048,576 字节，即 1 MiB**，这是本项目的策略。使用 `with ... stream(...)` 和 `iter_raw()` 逐块累计，**加入缓冲区前**检查；不依赖 `Content-Length`，超限后不消费后续块，不使用无界正文读取。
- 只有需要读取正文时才检查 HTTP `Content-Encoding`；允许缺省或去除两端空白、忽略大小写后为 `identity`，其他编码拒绝。该方法发送 `Accept-Encoding: identity`，不污染旧元数据方法的请求头。
- 正文严格按 UTF-8 解码并解析为 JSON 对象；无效编码、JSON、非对象或递归解析错误均拒绝。随后显式选择字段，用响应模型严格验证，不把整个上游对象直接返回；最终字符串也必须可严格编码为 UTF-8。
- `base` 和 `head` 必须包含所需的 `ref`、`sha`。head 可以来自 fork，不要求 `head.repo` 存在或非空，也不另查来源仓库。
- 上游 `number` 必须是真正整数且等于请求编号。模型中的 `html_url` 只要求非空，客户端先要求该链接只含 ASCII 字符，再与 `https://github.com/{owner}/{repo}/pull/{pull_number}` 作完整、大小写不敏感比较，不 strip。用户信息、端口、查询、片段、额外路径、末尾斜杠、百分号编码或反斜杠均不能混入。返回原 `html_url`，不请求它。标题和分支名继续支持 Unicode。
- 成功、超限、状态错误和读取异常时，响应流均由上下文管理释放；每次 API 请求创建的客户端也由已有依赖关闭，不创建模块级共享客户端。

这个 1 MiB 限制只属于 PR 方法，不替代文件接口的 HTTP 正文、Base64 或源码限制。仓库元数据方法仍没有相同的流式正文上限。

#### PR 错误响应

请求验证失败仍返回 HTTP 422，`detail` 为非空错误数组，错误项仅含 `loc`、`type`、`msg`，不回显原始输入。编号 `0` 的错误位置为 `["body","pull_number"]`；框架可能先构造依赖客户端，因此不能据此声称无效输入一定不会创建客户端，但不会调用 PR 查询方法，已创建的客户端仍会关闭。

PR 路由只转换查询调用阶段的下面七类具体异常，固定说明如下：

| 客户端异常 | HTTP 状态 | `detail` |
| --- | --- | --- |
| `GitHubNotFoundError` | `404` | `仓库或 PR 不存在或不可公开访问。` |
| `GitHubRateLimitError` | `503` | `GitHub 请求受到限流，请稍后重试。` |
| `GitHubForbiddenError` | `502` | `GitHub 拒绝访问该仓库或 PR。` |
| `GitHubRedirectError` | `502` | `GitHub 返回了重定向，请使用仓库当前的主页地址。` |
| `GitHubUpstreamError` | `502` | `GitHub 返回了无效或非预期的 PR 响应。` |
| `GitHubTimeoutError` | `504` | `GitHub 请求超时。` |
| `GitHubNetworkError` | `502` | `GitHub 网络请求失败。` |

状态分类先处理无需正文的情况：404 表示仓库或 PR 不存在或不可公开访问；429 为限流；403 且 `X-RateLimit-Remaining` 去除两端空白后为 `0` 时直接限流；300–399 不跟随重定向；其他非 200/403 状态归为上游异常。这些情况不读取正文。

200 和其余 403 才受限读取正文。403 的合法 JSON 对象中，字符串 `message` 包含 `api rate limit` 或 `secondary rate limit`（忽略大小写）时为限流；其他合法对象归为普通拒绝。**需要解析的 403 正文若是无效 JSON、非对象、无效 UTF-8、不支持的内容编码或超过上限，仍是上游异常 502**，不能笼统写成普通拒绝或限流。

PR 正文超限、字段无效、编号或链接身份不符均为 **502**，不是文件业务限制的 422。HTTPX 超时和其他请求异常分别转换为 504、502，范围覆盖连接、流式读取和上下文退出。路由不捕获客户端基类、`ValueError`、文件专用异常或所有程序异常；在查询的 `try/except` 外返回模型，不把 FastAPI 响应验证或其他程序错误包装成查询/文件下载业务错误，也不泄露底层异常文本或上游正文。

### 仓库元数据查询与文件审查的错误响应

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
- **固定 HTTP 请求目标。** 客户端只用已验证的 owner、repo、文件相对路径或严格 PR 编号构造 `/repos/{owner}/{repo}`、`/repos/{owner}/{repo}/contents/{file_path}` 或 `/repos/{owner}/{repo}/pulls/{pull_number}`，完整目标主机始终为 `https://api.github.com`。不请求用户提交的完整 URL，也不访问 `download_url`、PR `html_url` 等上游链接，不接受用户指定的 API 地址、请求头或认证信息。这用于防止 SSRF，即诱导服务器请求任意地址。
- **不跟随重定向，保持 TLS 证书验证。** 不访问上游 `Location` 指定的目标。
- **匿名读取公开信息。** 不需要配置 GitHub Token，也不发送 `Authorization`。匿名请求受到 GitHub API 限额约束，多个调用者可能共享服务器来源 IP 的额度；具体规则以 [GitHub 官方限额文档](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) 为准。
- **设置明确超时。** connect 为 3 秒，read、write、pool 各为 5 秒；这些是不同网络阶段的超时，不是整个请求总耗时最多 5 秒。
- **清理连接和响应流。** 三个 GitHub 路由复用同一依赖管理方式，每次请求创建客户端并在成功或异常结束时关闭；文件和 PR 读取的响应流也通过上下文管理释放。客户端支持 `with`、`close()` 和公开的 `is_closed`；模块导入时不创建长期存活的 HTTP 客户端。
- **限制错误信息。** 元数据经过响应模型验证，文件经过字段、大小和解码校验，PR 经过有界正文、严格模型和身份核对，明确的查询错误返回固定说明；原始错误正文和底层异常文本不直接透传。
- **校验文件链接标记但不作绝对保证。** 文件读取拒绝显式链接和子模块标记；Contents API 可能返回仓库内符号链接目标的普通文件内容，因此不能证明原路径一定不是链接。

URL、文件路径和 PR 编号纯验证函数本身不进行 DNS 或 HTTP 请求；仓库、文件或 PR 是否存在、是否可公开访问，需要后续实际 GitHub 响应确认。Python 源码只作为 AST 分析的数据，不执行、导入或保存。

## 当前限制与不支持的功能

- **仅审查 Python。** 接受提交的源码字符串，或公开仓库默认分支中的单个受限 Python 文件；不支持其他语言、整个仓库扫描、多文件批量审查或代码 diff。可解析的 Python 语法取决于运行服务的解释器版本。
- **规则只匹配语法结构。** 不追踪函数别名、变量绑定、跨文件关系或 `builtins.eval(...)`、`obj.print(...)` 等属性调用；自定义同名函数也可能被提示。
- **不是完整的 Python 正确性检查。** `PY_SYNTAX` 表示 AST 解析失败，不代表能发现所有编译阶段或运行时错误。注释和普通字符串中的规则名称不会触发问题；字符串插值表达式中的真实调用仍会被遍历。
- **提示需要结合上下文判断。** `print()` 有时是正常输出；`PY_PRINT` 只是提示，并不自动认定代码有缺陷。
- **不提供完整安全分析或安全保证。** 当前规则不是完整的漏洞扫描；不会在沙箱中执行源码，也不为每次审查请求启动独立进程。已有源码长度、远程文件字节和文件响应体大小限制，但不能替代完整的 CPU、内存、并发及总耗时资源隔离，提供 Docker 运行方式也不等于已有完整生产防护。
- **GitHub 范围受限。** 支持公开仓库元数据、默认分支单个 Python 文件读取审查和公开 PR 概览；不读取 PR 文件列表或 diff，不判断可合并性，不支持私有仓库、Token、webhook 或自动发布评论；GitHub Actions 仅负责验证本项目。
- **GitHub 客户端没有重试、缓存或认证。** 当前也没有调用方鉴权或应用级请求限流；文件有受限响应体和源码配额，PR 有独立 1 MiB 正文上限，元数据尚无相同的流式正文限制，不属于完整生产防护。
- **网络配置固定。** `trust_env=False`，不会自动采用环境代理或自定义证书变量；需要代理或企业证书的环境可能无法直接连接。2026-10-30 已完成最小应用实网验收，但期间也遇到匿名限流和连接超时，不保证所有网络环境或后续请求始终可用；其他机器访问和生产部署仍未验收。
- **本机监听验收范围有限。** Day 7—Day 9 当时只验证健康、文档及源码审查，未调用当时两个 GitHub 路由。Day 10 十项容器 HTTP 另增加 PR 非法编号的 422，未执行有效上游查询；不能据此声称有效 PR 实网或所有 GitHub 路由已验收，也不能推导其他机器一定不可访问。
- **CI 验证范围有限。** CI #11 已验证 Day 10 提交 `9ca16fe01532966fd1ce3dc44639b9272ca49c53` 的两个 job，不能自动覆盖后续提交。Day 9 原验收器退出 1 与后续端口补查分别保留。有效 PR 实网、Ubuntu 原生纯运行环境、镜像发布、其他机器访问和生产部署仍待单独验收。
- **没有 AI 审查。** 不调用 LLM（大语言模型）或付费 AI API，不生成 AI 修复建议。
- **没有产品前端、数据库或审查记录持久化。** `/docs` 是 FastAPI 提供的接口调试文档。

## 手动验证

以下为开发启动默认端口 `8000` 的手动示例。保持服务运行，在另一个 PowerShell 窗口执行；若采用上面的 `--port 0` 方式，先将 URL 中端口替换为本次启动日志的实际值：

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health'

$reviewResult = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:8000/api/v1/reviews' `
    -ContentType 'application/json' `
    -Body '{"code":"print(123)"}'
$reviewResult | ConvertTo-Json -Depth 5
```

第一个请求应返回 `status: ok`；第二个应返回 `language: python`、`issue_count: 1` 和一条 `PY_PRINT` 提示。也可在 `/docs` 页面直接试用接口。

上面的健康检查和源码提交接口不需要 GitHub 网络。访问 `/docs` 也不会自动查询 GitHub；在文档页面提交合法的元数据、文件审查或 PR 概览请求会发出真实 GitHub 请求。本批未执行这些命令；Day 5 最小 GitHub 实网验收和此后的本机 HTTP 结果分别见历史记录，不扩展为有效 PR 实网通过。

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

Day 10 的三份 PR 测试进一步覆盖严格编号、十四字段、Unicode 与 SHA、有界流读取、身份核对、状态分类、固定错误文本及响应/客户端关闭；API 测试保留非预期异常和响应验证失败，不把它们伪装成业务错误，并检查五路由 OpenAPI 和旧接口兼容性。

GitHub 测试阻断真实外部 HTTP 和 DNS；依赖覆盖结束时恢复，资源测试也通过替换构造器的传输层执行生产 `with/yield`，验证每次请求独立创建并关闭客户端。`test_smoke_http.py` 则允许临时标准库回环 HTTP 服务，用 `sys.executable -B` 运行真实脚本子进程，记录实际请求；错误路由、状态或字段位置必须失败，包含 `python -O` 反例。子进程有超时，服务在 `finally` 中关闭并确认线程退出及端口释放。这些模拟服务测试不导入应用，也不验证真实应用、GitHub、DNS、TLS 或部署。

单独运行 `scripts/smoke_http.py` 时需要已启动的本机服务和真实端口；当前检查十项，不使用 TestClient 或 MockTransport，包含非法 PR 编号请求，但不执行有效上游 GitHub 查询。Day 7—Day 9 当时九项脚本的历史结果保持原样；本批未运行测试或脚本。

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

这与 CI 中 Backend checks job 的检查内容和顺序一致：先独立执行 `pip check` 检查依赖冲突，再执行原四项检查。该 job 使用 setup-python 选出的解释器创建 `.venv`，之后安装和检查均显式使用 `.venv/bin/python`，不依赖跨步骤激活。Docker checks 是另一独立 job，不安装项目开发依赖。每条命令都应成功，不能只根据最后一条的结果判断所有检查通过。

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

下列 Day 4—Day 9 均为历史记录，其中“本批”等措辞沿用当时的文档批次语境，不表示本次 README 同步重新执行过这些检查。原数字、失败、警告、提交绑定和链接保留。

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

执行日期：2026-10-29（本机时区 UTC+08:00）。当时使用已有的 `backend/.venv`，实际解释器为 **Python 3.14.6**，覆盖率报告平台为 `win32`。CI 和静态检查的目标为 Python 3.12，该次本地验收没有切换解释器。这是此前记录，本批文档同步未重新执行。

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

以下为此前已核实的验收记录，本批仅同步 README，没有重新查询远程仓库或 CI：

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

本地记录为 Python 3.14.6、333 条语句，CI 记录为 Python 3.12.15、347 条语句；两者分别保留，没有调查或推测统计差异的原因。Day 6 的锁定依赖与 CI 验证另见下文绑定其完整 SHA 的 Backend CI #5 记录，不能将 Day 5 的结果算作 Day 6 的验收。

### Day 5 最小应用实网验收记录（历史）

日期：**2026-10-30，北京时间**。以下也是此前已核实记录，本批文档同步没有重新执行。验收通过进程内 FastAPI 接口流程，使用生产 GitHub 客户端和真实 GitHub HTTPS 请求：

| 场景 | 历史结果 |
| --- | --- |
| 健康接口 | HTTP 200 |
| 非法文件路径 | HTTP 422 |
| 读取 `psf/requests` 的 `src/requests/api.py` 并审查 | HTTP 200，`issue_count` 为 0 |
| 随机生成的不存在文件路径 | HTTP 404，固定错误说明正确 |

期间曾遇到匿名限流和连接超时，后续完成了上述成功场景；不能据此宣称网络始终稳定。验收没有启动监听服务，因此没有覆盖外部 HTTP 访问或部署验收，也不代表所有网络、代理、证书环境均已验证。

本地离线测试、远程 CI 和最小应用实网验收是三份独立记录。本地和 CI 中的自动化测试使用模拟上游响应；最小实网记录才覆盖上述真实 GitHub 上游访问场景。本批 README 同步没有重新执行这三项验收。

### Day 6 平台锁与全新环境验收记录（此前批次）

以下记录来自 2026-10-30 已完成的 Windows 第三批和 Debian Linux 第五批验收，**第六批及本批文档同步均未重跑这些验收**。运行环境、开发环境各自新建，未继承旧项目环境的包；安装均要求哈希和可用 wheel，并保持 TLS 校验。

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

第六批当时只调整 README 和 CI 配置，并做 YAML 与关键结构静态检查，没有重跑以上安装、冒烟、pytest、Ruff 或 mypy。后续已完成 Day 6 提交、推送和 Ubuntu 开发环境 CI，结果按完整提交 SHA 单独记录如下；本批仅同步这些已核实记录。

### Day 6 提交、推送与 Ubuntu CI 验收记录（历史，绑定指定提交）

以下采用用户提供的已核实记录，**本批没有重新查询远程或执行验收**：

- 提交及 CI `head_sha`：`2b0a4d9403d7831e04123c12181b6719eea75c79`。
- 提交信息：`build: separate and lock backend dependencies`。
- 已推送分支：`day4/github-api`。
- 运行：[Backend CI #5](https://github.com/tiezhi7/RepoPilot/actions/runs/41097662670)，Run ID 为 `41097662670`。
- 工作流文件：`.github/workflows/backend-ci.yml`；事件为 `push`，第 1 次运行。
- 最终状态：`completed / success`，`Backend checks` 全部步骤成功。

| 项目 | Day 6 Ubuntu 远程实际结果 |
| --- | --- |
| 环境 | Ubuntu 24.04.5 LTS，Python 3.12.16 x64 |
| 独立虚拟环境 | 创建 `backend/.venv`，使用该环境的 pip 26.1.2 |
| 严格依赖安装 | 从 `locks/linux-py312-dev.txt` 安装 39 个包，使用官方 PyPI、`--require-hashes` 和 `--only-binary=:all:` |
| `pip check` | `No broken requirements found.` |
| Ruff | `All checks passed!` |
| 格式检查 | `14 files already formatted` |
| mypy | 6 个源文件检查通过 |
| pytest | `691 passed in 3.03s`，未报告警告或跳过项 |
| `app` 行覆盖率 | 347 条语句、0 条未覆盖、100%，没有未覆盖行 |

x64 根据实际 Python 发行包和安装的 x86_64 wheel 核实；该记录不声称日志执行过 `uname -m`。覆盖率口径为行覆盖率，未收集分支覆盖率。

**该次 Ubuntu CI 验证的是开发锁的 39 包环境，没有单独验证 Ubuntu 纯运行环境。** Windows 和 Debian 的运行、开发环境结果仍以上一节各自的本地记录为准；两次运行环境 TestClient 弃用警告也继续保留，不因开发环境 CI 未报告警告而删除。

这是对应上述完整 SHA 的历史验收记录。后续仅修改或提交 README 时，无需改写本记录的 SHA；其他提交的 CI 结果应另行按其自身完整 SHA 记录，不能自动沿用本次成功结论。

该次 CI 使用离线自动化测试，没有进行应用真实访问 GitHub、监听服务、外部 HTTP 访问或部署验收。Day 5 已有最小应用实网历史记录继续保留。该次 CI 当时未覆盖的监听链路，后续已在 Day 7 第一批单独完成 Windows 本机验收；新依赖环境中的 GitHub 应用实网、其他机器访问和生产部署仍需单独安排。本批仅同步文档，没有重新执行安装、测试、CI 查询或应用实网请求。

### Day 7 Windows 本机 HTTP 监听验收记录（第一批已完成）

验收对象提交：`5fa9a40c1dd4f938a43737e892ebd637ff7f6663`。以下记录属于已完成的 Day 7 第一批；**当时的 Day 7 第二批仅同步 README，没有重新运行安装、HTTP 验收或历史检查，也没有查询 CI。Day 8 第三批及本批 Day 9 文档同步同样未重跑该次验收。**

实际环境为 **Windows AMD64 / CPython 3.14.6**，使用全新独立运行环境，未复用旧项目 `.venv`，未启用 system-site-packages。引导包 pip 固定为 **26.1.2**；从官方 PyPI 安装 Windows 运行锁，保持 TLS 校验，启用 `--require-hashes` 和 `--only-binary=:all:`。22 个运行包版本全部匹配锁文件，无开发依赖；`pip check` 通过，返回 `No broken requirements found.`。

自动验收以该新环境的绝对解释器路径启动独立 Uvicorn 进程，使用 `-B`、真实 `backend` 的 `--app-dir`、`--host 127.0.0.1`、`--port 0`、`--workers 1`，未启用 reload。工作目录、脚本、日志和缓存均放在专用临时目录；监听地址及实际服务 PID 的归属已核对。

请求使用普通 HTTPX Client，明确设置超时、`trust_env=False`、`follow_redirects=False`，经真实本机 TCP 访问服务。没有使用 `TestClient`、`ASGITransport`、`MockTransport` 或依赖覆盖替代网络链路。

| 实际场景 | HTTP 状态 | 响应断言结果 |
| --- | --- | --- |
| 初始 `GET /health` | `200` | JSON 为 `{"status":"ok"}` |
| `GET /docs` | `200` | 返回 Swagger UI HTML；只检查本地 HTML，未打开浏览器或加载 CDN |
| `GET /openapi.json` | `200` | 包含四个业务路由，`/health` 为 GET，其余三个为 POST |
| 提交 `x = 1` | `200` | `language: python`、`issue_count: 0`、`issues: []` |
| 提交 `print('day7')` | `200` | 恰好一个 `PY_PRINT`，`severity: info`，`line: 1` |
| 提交 `def broken(:` | `200` | 恰好一个 `PY_SYNTAX`，`severity: error` |
| 提交空字符串 | `422` | `detail` 为验证错误数组 |
| 提交含 `raise RuntimeError('must not execute')` 的源码 | `200` | 零问题，源码未执行 |
| 最后再次 `GET /health` | `200` | `status: ok`，服务仍正常 |

首次临时脚本把启动器 PID 当成实际监听 PID，导致 PID 核对失败，当时尚未发送 HTTP 请求。保留首次失败记录并清理该次服务后，仅修正临时脚本，通过父子进程关系确认实际监听 PID 的归属，随后九项真实响应断言全部通过；不能写成首次执行即全部通过。

清理结果：客户端在 `finally` 中关闭，通过 `CTRL_BREAK_EVENT` 发起服务关闭，未执行强制终止。**启动器返回码为 `3`；实际监听进程已退出，其独立退出码未单独记录。** 日志包含 `Application shutdown complete` 和 `Finished server process`，原监听已释放。这里保留实际返回码，不将 `3` 改写为 `0`，也不将其解释为已经证明的某种固定、正常 Uvicorn 退出码。

该次验收未发现 Python 或服务弃用警告；旧 `.venv` 历史退出 `103` 的原因仍未定位，未修复旧环境或修改其配置。项目 25 个文件的清单及 SHA-256 前后均未变，分支和提交不变，工作区干净、暂存区为空。

**结论仅适用于上述 Windows 新运行环境的本机 HTTP 监听链路。** 该次没有调用两个 GitHub 业务接口或请求真实 GitHub，没有验证其他机器访问或执行生产部署；不扩大为所有 GitHub 路由的真实 HTTP 行为已验收。Day 5 的真实 GitHub 历史记录、Day 6 的离线检查和本次本机 TCP 验收分别保留；CI #5 仍绑定其原提交，不用来代表此文档或其他提交的 CI。

### Day 8 本地 Docker 构建与容器 HTTP 验收记录（第一、第二批已完成）

业务源码基线为 `62967b6a9df9051543f17196fe17cfac697c9e60`。**验收时 `backend/Dockerfile` 和 `backend/.dockerignore` 仍是未跟踪文件，尚未提交；不能声称上述提交已经包含这两个文件。** 下列为此前第一、第二批的实际结果，Day 8 第三批当时只更新 README，没有重新构建、安装、启动容器、发送 HTTP 请求、运行其他测试或查询 CI；本批 Day 9 文档同步也未重跑这些验收。

已验收的本地镜像完整 ID：

```text
sha256:bdaad6147494002f0e3e21c9a9c8a8794c08faaee4acd15a99c61213fc7bfff8
```

这是本机镜像标识，不是公共仓库镜像地址；没有发布或推送镜像。

| 构建与镜像检查 | 已核实结果 |
| --- | --- |
| Docker Engine | `29.8.0`，使用 `desktop-linux` 引擎 |
| 镜像目标 | Debian 12 / Linux AMD64 / glibc 2.36 / CPython 3.12.16 |
| 基础镜像 | `python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337` |
| 构建方式 | 以 `backend` 为上下文，显式 `linux/amd64`，buildx 使用 `--load --progress plain`，唯一的本地标签 |
| 依赖 | `/opt/venv` 中 pip 26.1.2；23 个运行包逐项匹配 Linux 运行锁，无开发专用包或 pip-tools；`pip check` 退出 `0` |
| 应用内容 | 6 个源码文件的集合及字节哈希与主机一致，`app.main` 来自 `/app/app/main.py` |
| 运行配置 | 用户 `10001:10001`、工作目录 `/app`、声明 `8000/tcp`；JSON CMD 直接启动 Python/Uvicorn，单 worker、无 reload |
| 构建上下文 | 实际上下文导出验证通过，包括临时嵌套包和禁止项测试，不仅依赖静态 glob 推断 |
| 镜像内容检查 | 断网检查容器、不发布端口、无主机项目挂载；检查退出 `0`，容器已清理，成功镜像保留 |

前两次构建在 pip wheel 正文下载阶段超时；只给 Dockerfile 两处 pip 安装命令增加 `--timeout 60 --retries 2` 后，复验批次仅实际构建一次并成功，退出码为 `0`。这不证明网络故障根因已定位或修复。

非 root 用户执行 pip 检查时出现缓存目录 `/.cache/pip` 不可写警告，pip 自动禁用缓存，`pip check` 仍退出 `0`。该警告未被过滤，也未通过修改镜像、HOME 或新增可写目录消除；它与下列 HTTP 服务日志中未发现警告是不同记录。

第二批成功验收日期为 **2026-11-02（北京时间）**。使用同一完整镜像 ID 启动独立容器，保持默认 CMD、用户和工作目录，无主机挂载，采用正常 bridge 网络，仅将 `8000/tcp` 动态发布到 Windows 宿主机 `127.0.0.1`。请求由 Windows 普通 **.NET HttpClient** 发往宿主机实际端口，设置明确超时、禁用代理和自动重定向；未用 `TestClient`、`ASGITransport`、`MockTransport` 或依赖覆盖替代端口映射链路。

| 真实 HTTP 场景 | 状态码 | 明确断言结果 |
| --- | --- | --- |
| 初始 `GET /health` | `200` | JSON 为 `{"status":"ok"}` |
| `GET /docs` | `200` | Swagger UI HTML；未打开浏览器或加载外部 CDN |
| `GET /openapi.json` | `200` | 四个业务路由及方法正确，健康接口为 GET，其余三个为 POST |
| 提交 `x = 1` | `200` | `language: python`、零问题、`issues: []` |
| 提交 `print('day8')` | `200` | 恰好一个 `PY_PRINT`，`severity: info`，`line: 1` |
| 提交 `def broken(:` | `200` | 恰好一个 `PY_SYNTAX`，`severity: error` |
| 提交空字符串 | `422` | `detail` 为验证错误数组 |
| 提交含 `raise RuntimeError('must not execute')` 的源码 | `200` | 零问题；源码只作为字符串静态分析，没有执行 |
| 最后再次 `GET /health` | `200` | 服务仍正常 |

九项响应均通过断言。启动等待的前两次健康探测发生请求异常，第三次成功，不能写成首次探测即成功；该次服务日志未发现警告。没有调用两个 GitHub 业务接口，也没有访问真实 GitHub。

客户端在 `finally` 中关闭，随后保存服务日志并执行停止、状态采集和删除：

| 停止与清理项目 | 实际结果 |
| --- | --- |
| `docker stop -t 10` | 命令退出码 `0`，耗时 `1.27` 秒 |
| 容器最终状态 | `Running=false`、`Status=exited`、`State.ExitCode=0`、`OOMKilled=false` |
| 信号事件 | SIGTERM（15），未出现 SIGKILL |
| 关闭日志 | 包含 `Application shutdown complete` 和 `Finished server process` |
| 原宿主机端口 | 停止后拒绝连接，不再提供该服务 |
| 资源收尾 | 容器普通删除、镜像保留，没有强制清理，也未操作其他 Docker 资源 |

正常停止结论结合日志、事件、状态和端口结果，不仅依据退出码。成功批次重新建立了可跨工具调用读回的起始快照，27 个项目文件的路径集合、大小和 SHA-256 前后全部一致；这份比较不补证此前丢失起始快照的那一轮。

**结论仅覆盖该次 Windows 宿主机经 Docker Desktop 访问该 Linux 容器的本机 HTTP 链路。** 不扩大为 GitHub 业务实网、其他机器访问限制或生产部署验收，也不证明其他机器必然无法访问。Day 8 当时的 Backend CI 尚无 Docker 构建或容器 HTTP 步骤，旧 CI 成功不代表 Docker 已通过远程 CI。Day 9 后续增加的 Docker 检查已由下文 CI #9 按指定提交独立验证；本批只同步文档，没有重跑验收。

### Day 9 Docker CI 本地流程验收记录（2026-11-05，提交前历史）

业务源码基线为 `f292fb33ea509d5104b9f64a38feadc9511cedbc`，验收当时另外使用了**未提交的工作流和未跟踪的 `backend/scripts/smoke_http.py`**。该 SHA 不包含 Day 9 改动。以下是已有本地记录，Day 9 第三批当时只同步 README；本批同样没有重新构建、启动容器、发送 HTTP、执行端口补查或查询 CI，也没有修改原日志或 JSON。

| 环境 | 本地实际记录 |
| --- | --- |
| 宿主机 | Windows AMD64 / CPython 3.14.6，执行标准库 HTTP 脚本 |
| Docker CLI / Engine | 29.8.0，使用 `desktop-linux` 引擎 |
| buildx / Bash | 0.37.0 / 5.3.15 |
| 容器目标 | Debian 12 / Linux AMD64 / CPython 3.12.16，与宿主机脚本解释器分开记录 |
| 远程 Docker job 目标 | 当时仅配置 Ubuntu 24.04 / 宿主机 Python 3.12.16 x64；后续远程实际结果独立记录于下一节 |

该次本地镜像 ID（没有发布到镜像仓库）：

```text
sha256:96e8218791aad66d6998c7c51e52de394d87c26387ffd19e8e81254a19806af1
```

构建与修复按实际顺序记录：

1. 首次实际构建一次成功，退出 `0`，耗时 **103.742 秒**；安装及源码层命中缓存，不是全新依赖安装验收。
2. 随后的配置核对因镜像 `Config` 缺少 `Entrypoint` 键而触发 `KeyError`，启动核对步骤退出 `1`。原清理正文退出 `0`，该容器已普通删除，流程仍保留退出 `1`；这不是退出码 23 场景的记录。
3. 只将工作流中两侧 Entrypoint 访问改为 `config.get("Entrypoint") == image["Config"].get("Entrypoint")`。实际旧数据复现 `KeyError`，新表达式正确匹配，也拒绝不同的非空 Entrypoint；相对该修复批前副本恰好只有这一行变化。YAML、3 段内嵌 Python 和 5 段 Bash 语法检查通过。
4. 后续复用同一完整镜像 ID，新增构建次数为 **0**。Dockerfile、.dockerignore、Linux 运行锁和六个应用源码文件均与该次构建记录一致。

修复后重新从当前工作流提取启动、HTTP 和清理正文；Windows 路径与子进程环境作适配，原 Bash 正文保持不变。临时验收器在 `finally` 中调用原清理正文，没有仅依赖 GitHub 的 `always()` 表达式。

成功路径使用 Windows 宿主机普通标准库 HTTP 客户端，经 `127.0.0.1:51907` 的实际 Docker 端口映射访问服务，未使用 TestClient、ASGITransport、MockTransport、依赖覆盖或容器内部请求替代链路：

| 九项真实 HTTP 场景 | 状态码 | 已验证结果 |
| --- | --- | --- |
| 初始 `GET /health` | `200` | JSON 恰好为 `{"status":"ok"}` |
| `GET /docs` | `200` | Swagger UI HTML，未加载外部 CDN |
| `GET /openapi.json` | `200` | 四个业务路由和方法正确 |
| 提交 `x = 1` | `200` | `language: python`、`issue_count: 0`、`issues: []` |
| 提交 `print('day9')` | `200` | 恰好一个 `PY_PRINT`，`info`、第 1 行，数量与列表一致 |
| 提交 `def broken(:` | `200` | 恰好一个 `PY_SYNTAX`，`error`、有效行号，数量与列表一致 |
| 提交空 `code` | `422` | 非空验证错误数组，指向 `code` 字段 |
| 提交顶层 `raise RuntimeError("DAY9_MUST_NOT_EXECUTE")` 的源码 | `200` | 零问题，源码只作为 JSON 字符串提交和静态分析，未执行 |
| 最后再次 `GET /health` | `200` | JSON 正确，服务仍正常 |

该路径九项检查全部通过，冒烟脚本退出 `0`。上述端口是历史动态分配结果，不能当作后续固定端口。没有调用两个 GitHub 业务接口或访问真实 GitHub。

普通失败场景另启本批容器，经宿主机端口 `51155` 确认健康接口就绪后，注入步骤退出 `23`。两条容器流程分别保留结果：

| 流程 | 原始步骤结果 | 原清理正文退出码 | 流程最终结果 |
| --- | --- | --- | --- |
| 九项 HTTP 成功路径 | 启动核对 `0`，冒烟脚本 `0` | `0` | `0` |
| 普通失败后的清理 | 启动核对 `0`，健康就绪；注入步骤 `23` | `0` | **`23`** |

这两个容器均以 `docker stop -t 10` 正常停止；删除前状态为 `Running=false`、`Status=exited`、`State.ExitCode=0`、`OOMKilled=false`，日志均包含 `Application shutdown complete` 和 `Finished server process`。两个容器随后普通删除并确认不存在，镜像保留，没有强制清理或操作其他 Docker 资源。清理成功没有将原始失败结果改成成功；该场景只验证普通步骤失败，不证明 GitHub 取消、runner 崩溃或强制中断时必定收尾。

另用独立临时标准库服务验证 **HTTP 500 反例**，与真实容器成功结果分开记录：`/health` 返回正确的 `200` 和 JSON，`/docs` 故意返回 `500`。未经修改的冒烟脚本在文档检查处退出 **`1`**，只请求了 `/health`、`/docs`，没有输出“全部九项真实 HTTP 检查”通过。服务线程结束后，端口 `49247` 的原连接探测超时，释放状态当时未核实，因此**当批验收器最终退出 `1`**；该结果和原日志、JSON 均保留，没有写成整批全部通过。

**后续独立端口补查：** 根据本会话已核实记录，北京时间 **2026-11-05 22:19:45**，经正常审批执行 `Get-NetTCPConnection`，查询退出 `0`；端口 `49247` 的连接记录为空，监听数量为 `0`。这只证明补查时无监听，不追溯证明原探测时的状态，也不将原批次退出码改为 `0`；原超时原因仍未确定。补查来源是会话工具记录，不声称另有磁盘日志文件，本批未重新补查。

修复批的修改前、修复后及验收后快照均已独立读回：相对修改前只变更工作流这一行，其余 27 个文件未变；修复后至验收结束，28 个文件的清单、大小及 SHA-256 全部一致。该比较只适用于对应批次，不替代其他批次的起始记录。

**范围与历史状态：** 本地成功路径和普通失败收尾已有上述证据；HTTP 500 被拒绝、原端口探测超时和后续无监听分别保留。该次本地验收时，Day 9 工作流和脚本尚未提交或推送，远程 Docker CI 当时也未验证；后续提交、推送及 CI #9 结果独立记录如下，不追溯改写本地失败。镜像发布、GitHub 业务接口实网、其他机器访问和生产部署均不属于该次本地结论。Day 5 的应用实网历史结果仍单独保留，不能用本机冒烟替代。

### Day 9 提交、推送与远程 CI 验收记录（历史，绑定指定提交）

以下仅依据用户提供的已核实结果整理，**本批没有重新查询远程或执行验收**，没有补写运行开始或结束时间：

- 提交及 CI `head_sha`：`130d64ff90caf7dac2e7e78f4731f07f6d757315`，已推送至 `day4/github-api`。
- 运行：[Backend CI #9](https://github.com/tiezhi7/RepoPilot/actions/runs/42058547684)，Run ID 为 `42058547684`，attempt 为 `1`。
- 工作流文件：`.github/workflows/backend-ci.yml`；事件为 `push`，分支为 `day4/github-api`；`head_sha` 与上述完整提交完全一致。
- 最终状态：`completed / success`，Backend checks 和 Docker checks 两个 job 均成功。

#### Backend checks：远程实际记录

| 项目 | CI #9 结果 |
| --- | --- |
| 环境 | Ubuntu 24.04.5 LTS，CPython 3.12.16 x64 |
| 独立虚拟环境 | `backend/.venv`，pip 26.1.2 |
| 严格依赖安装 | 从 Linux 开发锁安装 39 包，启用哈希校验和仅 wheel 限制 |
| `pip check` | `No broken requirements found.` |
| Ruff | 通过 |
| 格式检查 | `15 files already formatted` |
| mypy | 6 个源文件通过 |
| pytest | `691 passed in 4.25s`，未报告警告或跳过项 |
| `app` 行覆盖率 | 347 条语句、0 未覆盖、100%，无未覆盖行；未收集分支覆盖率 |

#### Docker checks：远程实际记录

| 项目 | CI #9 结果 |
| --- | --- |
| Runner | Ubuntu 24.04.5，与容器环境分开记录 |
| Docker CLI / Engine | 28.0.4 |
| buildx / 宿主机 Python | 0.38.1 / 3.12.16 |
| 构建 | 成功；日志没有 `CACHED` 标记，虚拟环境创建、依赖安装及源码复制均实际执行，不据此断言所有构建环节绝无缓存 |
| 镜像身份 | 本次 Build IID 与实际镜像 ID 一致，完整值见下方；容器按本次构建 ID 启动，不要求等于本地历史镜像 ID |
| 容器配置 | 默认 CMD、用户 `10001:10001`、工作目录 `/app`、无挂载及唯一端口映射均核对通过 |
| 历史动态地址 | `127.0.0.1:32768`，仅属于该次运行，不作为固定使用地址 |
| 就绪探测 | 前两次连接被重置，第三次成功 |
| 九项真实 HTTP | 初始健康、Swagger HTML、OpenAPI、正常源码、print、非法语法、空 code、源码不执行、最后健康均通过；空 `code` 为 `422`，其余八项为 `200` |
| 清理步骤 | 实际执行成功；`docker stop` 未进入失败分支，但日志未单独打印其数值退出码，不将它与容器自身退出码混为一谈 |
| 容器停止状态 | `Running=false`、`Status=exited`、`ExitCode=0`、`OOMKilled=false` |
| 关闭日志 | 包含 `Application shutdown complete` 和 `Finished server process` |
| 删除确认 | 普通删除及删除后残留检查通过 |

该次远程 Build IID 和实际镜像 ID 的共同值为：

```text
sha256:4291af53c347708cf87ab8f97aab02c0670753912d18054de3267c1484f12702
```

**验证边界：** Ubuntu 是 runner 的系统。Dockerfile 指定的容器目标仍为 Debian 12 / Linux AMD64 / CPython 3.12.16，但该次远程日志没有单独查询输出容器内完整 Python 版本或系统信息，不能写成这些信息已独立核实。该次 Docker HTTP 与清理步骤成功，不代表已单独验证 Ubuntu 原生纯运行环境；Debian 容器或 Backend checks 的 39 包开发环境均不能替代它。

远程工作流没有重新执行本地退出码 23 和 HTTP 500 两个独立反例，也不改写本地原验收器退出码 1 或端口补查的历史事实。镜像未发布，GitHub 业务接口实网、其他机器访问及生产部署不属于 CI #9 的结论；Day 5 的应用实网历史记录继续单独保留。

**CI #9 永久绑定上述完整 SHA，不自动代表后续文档提交。** 后续提交应按自身 SHA 核对 CI，不预填新运行编号或结果，也无需为每次后续文档 CI 再追加历史记录。本批只同步文档，没有重新安装、测试、构建、启动服务或联网查询。

### Day 10 本地验收记录（第一至第七批，ASCII 修复前，验收时尚未提交）

以下保留第八批当时整理的本地验收结果，第八批仅同步 README，没有重新执行验收。验收基线为分支 `day4/github-api` 的 HEAD `27d0bdc4842c37a2bf735ad629ca77016667ff89` 加当时八项未提交改动，包括三个应用文件、冒烟脚本及四份新测试。该旧 HEAD 本身不包含 Day 10 功能。当时 README 更新后累计九项改动，尚未提交、推送或取得对应远程结果。以下两平台 1309 项全量验收及本地运行镜像均属于 ASCII 修复前记录，后续修复、提交和 CI #11 另节记录。

#### 前四批：非锁定环境检查与诊断

前四批使用已有的 Anaconda / Windows AMD64 / Python 3.13.9，工具包括 pytest 8.4.2、Ruff 0.12.0、mypy 1.17.1；这是非平台锁定环境，不替代后续全新环境验收。

| 批次 | 已有实际测试记录 |
| --- | --- |
| 第一批：PR 模型 | 新测试 274 passed；连同两个旧模型模块的相关回归 532 passed |
| 第二批：PR 客户端 | 新测试 257 passed；相关客户端、文件客户端及 PR 模型回归 788 passed |
| 第三批：PR API | 新测试 74 passed；相关 API 回归 242 passed；两次 pytest 输出各保留 1 条 TestClient/httpx 弃用警告 |
| 第四批：冒烟脚本 | 最终 13 passed in 6.01s；包含成功、失败和 `python -O` 场景，不代表真实应用已验收 |

第三批当时没有 httpx2，保留 `StarletteDeprecationWarning`，内容为使用 httpx 搭配 `starlette.testclient` 已弃用、应安装 httpx2；未过滤或临时补装依赖消除它。后来锁定开发环境没有测试警告，不追溯抹去该记录。

这些批次并非所有命令首次就成功：第三批曾无输出中断，原因未确定；第四批最初的模拟服务使用 HTTP/1.0，触发 `WinError 10038` 和子进程超时，端口连接探测也曾超时。仅修正新测试的 HTTP/1.1 模拟及以重新独占绑定确认端口释放，并处理允许文件的排版差异后完成验证，没有削弱生产检查条件。原失败和诊断记录保留。

#### 第五、第六批：两平台全新锁定开发环境

两平台各自创建独立新 venv，未继承旧环境或用户 site-packages。使用官方 PyPI、TLS 校验、`--require-hashes` 和 `--only-binary=:all:`，完整安装对应开发锁；引导包 pip 固定为 26.1.2，安装日志与报告单独记录，不宣称它也经过项目锁文件校验。

| 项目 | Windows 第五批实际结果 | Debian Linux 第六批实际结果 |
| --- | --- | --- |
| 环境 | Windows AMD64 / CPython 3.14.6 | Debian 12/bookworm / Linux x86_64 / glibc 2.36 / CPython 3.12.16 |
| 新开发环境依赖 | 39 个锁定包逐项版本一致；锁外只有 pip 26.1.2，无 pip-tools | 39 个锁定包逐项版本一致；锁外只有 pip 26.1.2，无 pip-tools；uvloop 0.23.0 存在，Linux 条件依赖已核对 |
| 严格安装、版本核对、`pip check` | 均退出 0；无损坏依赖 | 均退出 0；无损坏依赖 |
| Ruff / 格式 / mypy | 均退出 0；19 个 Python 文件符合格式，6 个应用源文件通过类型检查 | 均退出 0；19 个 Python 文件符合格式，6 个应用源文件通过类型检查 |
| 全量 pytest | `1309 passed in 30.31s`；无失败、跳过或测试警告，退出 0 | `1309 passed in 19.85s`；无失败、跳过或测试警告，退出 0 |
| `app` 行覆盖率 | 462 条语句、0 未覆盖、100%，无未覆盖行 | 479 条语句、0 未覆盖、100%，无未覆盖行 |
| 检查网络边界 | GitHub 测试使用模拟上游，冒烟脚本测试仅使用临时回环服务 | 安装后断开本批容器全部外部网络端点，inspect 为 `{}`；回环网络保留供测试使用 |

四份 Day 10 新测试均参与全量执行：模型 274、客户端 257、API 74、冒烟脚本 13。没有筛选、排除或过滤警告，也没有自动重跑整套质量检查。两个报告均为 **app 行覆盖率，未收集分支覆盖率**；不推测两平台语句数差异的原因，100% 不等于所有安全边界均已证明。

Debian 使用前文固定基础镜像摘要，完整 Python 为 `3.12.16 (main, Oct 28 2026, 02:01:35) [GCC 12.2.0]`；宿主机用于控制 Docker 的 Windows Python 3.14.6 与容器解释器分开记录。项目 backend 只读挂载，虚拟环境、缓存和日志放在容器临时目录或项目外，临时容器已普通停止、删除。

Linux 收尾辅助脚本曾在 Windows 默认 GBK 编码下读取 UTF-8 安装报告失败，退出 1；只修正项目外脚本的读取编码后核对退出 0，原错误保留。这不是重新安装或重跑质量检查，也不将整个 Day 10 写成所有命令首次全部成功。两批最终确认 32 个项目文件、忽略清单、分支、HEAD 和暂存区均未改变，没有在项目内新增缓存或报告；Git 的 LF/CRLF 提示保留，未改写换行配置。

#### 第七批：运行镜像重建、内容与十项真实 HTTP

以下同样是此前已完成记录，基于上述旧 HEAD 加八项未提交改动。Docker CLI/Engine 为 29.8.0，buildx 为 0.37.0，使用 `desktop-linux`，以 `backend` 为唯一构建上下文，显式 `linux/amd64`、`--load --progress plain`、唯一标签及项目外 `--iidfile`。

实际只构建 **1 次**，退出 0，耗时 **92.596 秒**。venv、运行锁及依赖安装层命中缓存，源码复制层实际执行；**不是全新依赖安装验收**。构建 IID 与 image inspect 的完整镜像 ID 一致：

```text
sha256:10545892784e3a087e155b1f6a3687a56272ee98ea728cdc0bdd9fc6fc9df216
```

这是保留的本地镜像 ID，不是已发布的公共镜像地址。按该 ID 启动本批唯一归属容器，保持默认 CMD/Entrypoint、用户 `10001:10001` 和工作目录 `/app`，bridge 网络、无挂载、无额外 init/特权/重启策略，只动态发布 `127.0.0.1::8000/tcp`。Entrypoint 采用两侧 `.get()` 比较，兼容键省略或 null。

| 镜像内容核对 | 实际结果 |
| --- | --- |
| 系统与解释器 | Debian 12 / Linux x86_64 / glibc 2.36；完整 CPython `3.12.16 (main, Oct 28 2026, 02:01:35) [GCC 12.2.0]` |
| 虚拟环境和身份 | `/opt/venv/bin/python`、`sys.prefix=/opt/venv`；实际 UID/GID 均为 10001 |
| 依赖 | 23 个 Linux 运行锁包逐项一致，锁外只有 pip 26.1.2，无开发专用包或 pip-tools |
| `pip check` | 退出 0，`No broken requirements found.` |
| 应用来源 | `app.main` 来自 `/app/app/main.py`；六个应用源码文件的集合及字节 SHA-256 与主机起始快照一致 |
| 复制的运行锁 | `/tmp/linux-py312-runtime.txt` 的字节哈希与主机一致 |

非 root pip 检查仍出现 `/.cache/pip` 不可写、缓存自动禁用的警告，未过滤或修改镜像消除。**本次内容检查使用 bridge 网络，不是断网容器检查**，也没有安装额外依赖。

宿主机解释器重新核实为 Windows AMD64 / CPython 3.14.6，直接运行仓库原版 `scripts/smoke_http.py`，经真实 TCP 访问 Docker 发布的本机端口，没有使用 TestClient、MockTransport、模拟服务或改写的脚本副本。该次地址为 `127.0.0.1:53067`，是历史动态端口，不是固定配置。

| 十项真实 HTTP 场景 | 实际状态 | 已通过断言 |
| --- | --- | --- |
| 初始健康检查 | 200 | JSON 恰好为 `{"status":"ok"}` |
| Swagger HTML | 200 | 包含 Swagger UI，未加载外部 CDN |
| OpenAPI | 200 | 五个业务路由及对应方法存在 |
| 正常源码 | 200 | python、零问题、空 issues |
| print 调用 | 200 | 恰好一个 PY_PRINT，info、第 1 行，数量一致 |
| 非法语法 | 200 | 恰好一个 PY_SYNTAX，error、有效行号，数量一致 |
| 空 code | 422 | 非空验证错误数组，指向 code |
| 含顶层 raise 的源码 | 200 | 零问题，源码只作为数据提交、未执行 |
| PR 编号 0 | 422 | 非空错误数组，含精确 `loc=["body","pull_number"]` |
| 最后健康检查 | 200 | 正文正确，服务仍正常 |

原版脚本只执行 **1 次**，退出 0，耗时 **1.516 秒**，stderr 为空，无失败就绪探测；合计八项 200、两项 422。不提交有效 GitHub 查询；PR 的 422 只证明输入边界，不代表有效 PR 查询或 GitHub 实网验收。

`finally` 核对本批容器完整 ID、名称及归属标签后保存日志，执行 `docker stop -t 10`：命令退出 0，耗时 **1.270 秒**。容器自身为 `Running=false`、`Status=exited`、`ExitCode=0`、`OOMKilled=false`；日志含 `Application shutdown complete` 和 `Finished server process`。容器普通删除及无残留查询通过，Windows TCP 监听表确认原端口无监听，未将连接超时当作释放证据；没有强制删除或清理其他资源，成功镜像和项目外日志保留。

第七批准备操作曾因自动审批 403 未启动，没有进程退出码，也不计作实际构建；经用户继续指令后，沿原工具和正常审批流程恢复，实际构建仍只有上述一次。最终 32 个项目文件的集合、大小和 SHA-256、忽略清单及 Git 状态均保持不变。

**边界：** 上述结果覆盖 ASCII 修复前的两平台开发环境和 Windows 经 Docker Desktop 访问当时镜像的本机链路。当时尚未取得 Day 10 Ubuntu 远程 CI 结果，后续 CI #11 按修复后的完整提交另节记录。有效 PR 实网、Ubuntu 原生纯运行环境、镜像发布、其他机器访问和生产部署仍待单独验收。Day 5 的既有实网历史记录仍单独保留，Day 7—Day 9 当时四路由、九项冒烟的记录没有改写。

### Day 10 ASCII 链接修复与定向回归记录（历史）

完整差异审查发现 PR 链接身份核对存在一项 P2 问题。Python 的 Unicode `lower()` 可能将非 ASCII 字符转为 ASCII 字母，导致不同原始链接比较相等。客户端已增加 `html_url.isascii()` 检查，再忽略大小写比较完整链接，保留通过核对的原值。标题和分支名仍允许 Unicode。

修复集中在 `backend/app/github_client.py` 及现有 PR 客户端测试。新增回归用例先复现旧行为，修复后通过，已有合法 ASCII 大小写链接和 Unicode 元数据测试继续通过。

| 定向回归 | 修复后已有实际记录 |
| --- | --- |
| 环境 | Windows 锁定开发环境，CPython 3.14.6 |
| PR 客户端和模型 | 合计 `532 passed` |
| PR API 集成 | `74 passed` |
| Ruff、格式检查及 mypy | 均通过，检查范围沿用该次定向回归记录 |

首次合并运行进入 API 测试后停滞并中止，原因未确定。随后单独运行 API 集成测试通过，保留两次运行各自的实际结果。该次没有重新执行两平台全量验收或重建运行镜像，上文 1309 项全量测试的数字、警告和本地镜像 ID 均属于修复前版本。修复已纳入下述 Day 10 提交。

### Day 10 提交、推送与远程 CI 验收记录（历史，绑定指定提交）

以下依据用户提供的已核实结果整理，本批只同步 README 并做静态检查，没有重新推送、查询 CI 或执行验收。

- 提交为 `9ca16fe01532966fd1ce3dc44639b9272ca49c53`，提交信息为 `feat: add read-only GitHub pull request overview`。
- 直接父提交为 `27d0bdc4842c37a2bf735ad629ca77016667ff89`，已推送分支为 `day4/github-api`。
- 运行记录为 [Backend CI #11](https://github.com/tiezhi7/RepoPilot/actions/runs/42985490946)，Run ID 为 `42985490946`，run number 为 `11`，attempt 为 `1`。
- 工作流为 `.github/workflows/backend-ci.yml`，事件为 `push`，`head_branch` 为 `day4/github-api`，`head_sha` 与上述完整提交 SHA 一致。
- 最终状态为 `completed / success`，Backend checks 和 Docker checks 均成功。

#### Git 推送经过

此前推送曾报告 `Recv failure: Connection was reset`，连接器随后确认远端仍为父提交。本次恢复期间，直接 Git 只读查询也曾无法连接 `github.com:443` 并退出 1。Windows 系统代理 API 确认既有 HTTP 代理为 `http://127.0.0.1:7892`，通过该路径的只读查询退出 0。

随后仅让本次 Git 子进程临时使用该代理，指定完整 SHA 和完整目标引用的推送命令执行一次，退出 0。连接器实时确认远端 `refs/heads/day4/github-api` 已指向 `9ca16fe01532966fd1ce3dc44639b9272ca49c53`。持久 Git 和代理配置保持原样。该地址只记录当时的 Git 推送路径，路径当时可用，连接重置的根因仍未确定。

#### Backend checks 远程实际记录

| 项目 | CI #11 结果 |
| --- | --- |
| 系统与解释器 | Ubuntu 24.04.5 LTS，CPython 3.12.16 x64 |
| 虚拟环境 | 独立 `backend/.venv`，pip 26.1.2 |
| 项目依赖 | 从官方 PyPI 严格安装 Linux 开发锁 39 包，使用 `--require-hashes --only-binary=:all:` |
| `pip check` | `No broken requirements found.` |
| Ruff | `All checks passed!` |
| 格式检查 | `19 files already formatted` |
| mypy | 6 个应用源文件通过 |
| pytest | `1310 passed in 8.02s`，无失败、跳过或测试警告 |
| `app` 行覆盖率 | 479 条语句、0 未覆盖、100%，无未覆盖行，未收集分支覆盖率 |

以上是修复后提交在 Ubuntu 开发环境中的本次远程结果，上文两平台修复前 1309 项记录保持原值。

#### Docker checks 远程实际记录

| 项目 | CI #11 结果 |
| --- | --- |
| 工具版本 | Docker CLI/Engine 28.0.4，buildx 0.38.1，宿主机 Python 3.12.16 |
| 构建 | 成功，未见 `CACHED` 标记，venv 创建、严格运行锁安装和源码复制均实际执行，构建中的 `pip check` 通过 |
| 镜像身份 | Build IID 与本次实际镜像 ID 一致，完整值见下方 |
| 容器归属与配置 | 核对通过，保持默认 CMD/Entrypoint、用户 `10001:10001`、工作目录 `/app`，无挂载及唯一动态端口映射 |
| 历史动态地址 | `127.0.0.1:32768`，不是固定使用地址 |
| 就绪探测 | 前两次连接重置，第三次成功 |
| 真实 HTTP | 十项通过，包含五路由 OpenAPI，空 code 和 PR 编号 0 为 422，其余八项为 200 |
| 停止步骤 | `docker stop` 未进入失败分支，其数值退出码未单独打印 |
| 容器自身状态 | `Running=false`、`Status=exited`、`ExitCode=0`、`OOMKilled=false` |
| 关闭日志 | 包含 `Application shutdown complete` 和 `Finished server process` |
| 删除与残留检查 | 本批容器普通删除，删除后残留检查通过，清理步骤成功 |

Build IID 和实际镜像 ID 的共同值为

```text
sha256:261cab9fad05bc8bc0507b4aa5152ad74d86f9648d1b92f3beeadedce115e884
```

日志中的构建执行记录不等于所有环节绝无缓存。容器内完整 Python 版本及系统信息没有单独查询，Ubuntu runner 与 Dockerfile 指定的容器目标分别记录。容器自身退出码 0 与 `docker stop` 命令未单独打印的数值退出码也分别保留。

十项 HTTP 检查中的 PR 编号 0 只验证输入边界，未执行有效 PR 查询。有效 PR 实网、Ubuntu 原生纯运行环境、镜像发布、其他机器访问和生产部署继续待单独验收。

**CI #11 永久绑定 `9ca16fe01532966fd1ce3dc44639b9272ca49c53`。** 后续文档提交按自身 SHA 验收，不能自动沿用此次成功，也无需为每次文档 CI 追加历史节。

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

当前工作流未设置分支或路径过滤，包含 `backend` job（显示名称 **Backend checks**）和 `docker` job（显示名称 **Docker checks**）。两个 job 不设置 `needs` 依赖，各自独立执行。工作流保持只读权限；两个 job 均使用 `runs-on: ubuntu-24.04`、原有 `actions/checkout@v7` 和 `actions/setup-python@v7`，checkout 配置 `persist-credentials: false`，setup-python 指定 `python-version: "3.12.16"`、`architecture: "x64"`。

Backend checks 的全部 `run` 步骤工作目录仍为 `backend`，原行为与检查参数不变。先使用 setup-python 提供的 `python` 创建新虚拟环境；随后均显式使用 `.venv/bin/python`：

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

工作流仅申请 `permissions: contents: read`，无需手动配置密钥。没有增加矩阵、缓存、部署或发布；任意安装、哈希校验或检查失败都会使对应 job 失败，未设置忽略失败。Backend checks 的覆盖率仍只测量行覆盖率，没有最低覆盖率门槛。

### Docker checks：当前配置与已验证范围

Docker job 总时限为 **20 分钟**，显式设置 Bash 和默认工作目录 `backend`，不依赖另一个 job 的 defaults。该 job 的宿主机 Python 目标为 **3.12.16 x64**，HTTP 脚本只用标准库，不在宿主机安装项目开发依赖；镜像内 Python 和依赖由 Dockerfile 与 Linux 运行锁决定。

| 步骤 | 当前配置 |
| --- | --- |
| 工具版本 | 输出 Docker、buildx 和宿主机 Python 版本 |
| 构建 | 在 `backend` 使用 Dockerfile 和当前目录上下文，执行 `docker buildx build --platform linux/amd64 --load --progress plain`；使用本次运行的唯一标签和 `--iidfile`，构建步骤时限 15 分钟 |
| 镜像身份 | 从构建 IID 核实实际本地镜像 ID，保存于 `RUNNER_TEMP` 的本次状态目录；按该 ID 启动，不使用历史镜像 ID |
| 容器启动 | `--pull=never`、唯一名称及 `io.repopilot.ci-owner` 归属标签、`--cidfile`、bridge 网络，仅动态发布 `127.0.0.1::8000/tcp`；保留默认 CMD、用户和工作目录，无挂载，不使用 `--rm` |
| 配置核对 | 检查完整 ID、名称、归属标签、镜像 ID、用户 `10001:10001`、工作目录 `/app`、默认 CMD/Entrypoint、无挂载及唯一端口映射，从实际 inspect 读取并校验宿主机端口 |
| HTTP 冒烟 | 在 `backend` 执行 `python -B scripts/smoke_http.py --port <实际宿主机端口>`；步骤时限 2 分钟，读取当前提交的十项脚本，包含非法 PR 编号请求，不执行有效上游 GitHub 查询 |
| 日志与清理 | 独立 `if: always()` 步骤，时限 3 分钟，工作目录改为 `${{ runner.temp }}`，不依赖源码目录存在；核对本次容器归属，收集停止前后日志，执行 `docker stop -t 10`，读取停止状态后普通删除本批已停止容器 |

冒烟脚本只允许端口 `1–65535`，目标主机固定为 `127.0.0.1`；使用 `http.client` 建立真实 HTTP 连接，不使用环境代理或自动跟随重定向，连接在成功或异常时关闭。连接和读写操作设置最长 5 秒超时，就绪探测按剩余期限收紧，就绪等待最多 45 秒；仅就绪探测有限重试，业务响应不符合预期立即非零退出，逐项输出检查名称、状态码和结果。

正常停止要求 `Running=false`、`Status=exited`、`ExitCode=0`、`OOMKilled=false`，并核对 `Application shutdown complete` 和 `Finished server process` 日志。清理按唯一名称、归属标签和完整 ID 确认本次容器，未创建容器时明确输出无需清理；仅在确认已停止后普通删除，不强制删除、不清理其他资源。普通步骤失败时仍收尾，清理成功不会覆盖原失败结果，停止、状态或清理异常也会保留失败结论；**runner 被强制终止时不能保证 `always()` 步骤执行**。

构建阶段需要访问基础镜像源和 PyPI；HTTP 场景仅访问本机端口，不加载外部 CDN。当前脚本会调用 PR 业务路由的非法输入边界，但不执行有效上游 GitHub 查询，因此不能将整个 job 称为完全断网验收。没有镜像发布步骤。

**Day 9 当时的 Docker 配置和九项脚本随提交 `130d64ff90caf7dac2e7e78f4731f07f6d757315` 推送，并通过 Backend CI #9 的两个 job。** 当前工作流文件没有因 Day 10 改动，但会调用仓库相应版本的脚本。Day 10 的十项脚本已随提交 `9ca16fe01532966fd1ce3dc44639b9272ca49c53` 推送，两个 job 均在 Backend CI #11 中成功。旧 CI #9/#10 仍只对应其自身提交。本批仅依据已有记录同步 README，没有修改 CI、重新查询远程或执行验收。

**此前 Day 4 远程验收记录：** 提交 `d584a95d48281ec19fa3429f90378366f2081c88` 已推送，Backend CI 成功，见[历史运行记录](https://github.com/tiezhi7/RepoPilot/actions/runs/40859087524)。该链接只代表 Day 4；Day 5 的成功运行是上文另列的 Backend CI #4。**本批文档同步未访问或重新查询这些运行。** CI 成功本身不能替代应用实网验收，后者按上文记录的场景单独完成。

Day 4、Day 5 和 Day 6 的远程结果分别绑定各自历史提交。Day 6 的依赖声明、四份锁、CI 和文档调整对应提交 `2b0a4d9403d7831e04123c12181b6719eea75c79`，已推送并通过 Backend CI #5 的 Ubuntu 开发环境验证。Day 7 本机 HTTP 记录另绑定 `5fa9a40c1dd4f938a43737e892ebd637ff7f6663`。本批 README 同步属于此后的独立文档改动，不改变历史运行的 `head_sha`，也不把已有 CI 成功结论自动延伸到其他提交。

查看已记录的运行时，可在仓库页面选择 **Actions → Backend CI → 对应运行记录 → Backend checks**，核对完整提交 SHA，再展开环境版本、严格安装、`pip check` 和四项检查的实际日志。查看 CI #9 或 CI #11 时还需单独检查 **Docker checks** 的构建、HTTP 与清理步骤。后续提交同样按自身 SHA 核对，不能用另一个 job 或旧 SHA 的成功代替。本批没有重新访问这些页面。参见 [GitHub 查看工作流运行历史](https://docs.github.com/en/actions/how-tos/monitor-workflows/view-workflow-run-history)。

## 下一阶段计划（尚未实施）

Day 4—Day 9 的历史提交、CI 和验收继续按各自记录保留，尤其 Day 9 原验收器退出 `1` 与后续端口补查仍分别记录。Day 10 已完成 PR 概览、五路由/十项冒烟、修复前两平台全量及本地镜像验收、ASCII 修复与定向回归，并已提交、推送，由指定 SHA 的 CI #11 验证两个 job。本批仅同步 README，后续按独立授权范围进行。

1. 下一步只读复核本次 README 差异，核对当前说明、历史绑定和验证边界。
2. 复核通过后再单独安排文档提交、推送及对应 SHA 的 CI 验收，无需为每次后续文档 CI 追加历史节。
3. 单独安排有效 PR 的 GitHub 实网验收，明确网络、限额及测试对象；PR 编号 0 的本机 422 不替代它，Day 5 实网历史记录也不扩展为 PR 已验证。
4. 如需验证 Ubuntu 原生纯运行环境，另建干净环境仅安装运行锁并独立验收，不能以 39 包开发环境或 Debian 容器代替。
5. 镜像发布、其他机器访问/访问限制和生产部署分别确定范围并授权验收，不从本机 `127.0.0.1`、离线测试或历史 CI 推导通过。

应用级限流、缓存和覆盖率门槛也需要后续单独评估；本批未添加。

PR 文件列表、diff 和多文件审查、整仓库扫描、AI 审查、私有仓库与 Token、评论发布、前端和数据库仍属于后续需要单独设计与确认的工作，不重复安排已经实现的 PR 只读概览。
