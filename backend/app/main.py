"""FastAPI 入口：接收请求，调用审查逻辑，再返回结构化结果。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .github_client import (
    GitHubFileTooLargeError,
    GitHubForbiddenError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRedirectError,
    GitHubRepositoryClient,
    GitHubTimeoutError,
    GitHubUnsupportedFileError,
    GitHubUpstreamError,
)
from .reviewer import review_python
from .schemas import (
    GitHubFileReviewRequest,
    GitHubFileReviewResponse,
    GitHubRepositoryRequest,
    GitHubRepositoryResponse,
    ReviewRequest,
    ReviewResponse,
)

app = FastAPI(title="RepoPilot")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, error: RequestValidationError
) -> JSONResponse:
    # 不把原始输入放进错误响应，避免非法 Unicode 再次导致 JSON 编码失败。
    details = [
        {"loc": issue["loc"], "type": issue["type"], "msg": issue["msg"]}
        for issue in error.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/reviews", response_model=ReviewResponse)
def create_review(request: ReviewRequest) -> ReviewResponse:
    # FastAPI 会先校验请求；缺少 code、类型不符或长度越界时自动返回 422。
    issues = review_python(request.code)
    return ReviewResponse(
        language="python",
        issue_count=len(issues),
        issues=issues,
    )


def get_github_repository_client() -> Iterator[GitHubRepositoryClient]:
    """每次请求创建客户端；yield 后的清理在成功或异常时都会执行。"""
    with GitHubRepositoryClient() as client:
        yield client


@app.post("/api/v1/github/repository", response_model=GitHubRepositoryResponse)
def lookup_github_repository(
    request: GitHubRepositoryRequest,
    client: Annotated[GitHubRepositoryClient, Depends(get_github_repository_client)],
) -> GitHubRepositoryResponse:
    """校验请求后查询公开仓库，将明确的客户端错误映射成 HTTP 错误。"""
    try:
        return client.fetch_repository(request.repository_url)
    # 这些具体异常是同一基类的不同子类；不提前捕获基类或所有异常。
    # 对外使用固定说明，不能直接返回 str(error) 或底层异常链。
    except GitHubNotFoundError:
        raise HTTPException(status_code=404, detail="仓库不存在或不可公开访问。") from None
    except GitHubRateLimitError:
        raise HTTPException(status_code=503, detail="GitHub 请求受到限流，请稍后重试。") from None
    except GitHubForbiddenError:
        raise HTTPException(status_code=502, detail="GitHub 拒绝访问该仓库。") from None
    except GitHubRedirectError:
        raise HTTPException(
            status_code=502, detail="GitHub 返回了重定向，请使用仓库当前的主页地址。"
        ) from None
    except GitHubUpstreamError:
        raise HTTPException(status_code=502, detail="GitHub 返回了无效或非预期的响应。") from None
    except GitHubTimeoutError:
        raise HTTPException(status_code=504, detail="GitHub 请求超时。") from None
    except GitHubNetworkError:
        raise HTTPException(status_code=502, detail="GitHub 网络请求失败。") from None


@app.post("/api/v1/github/file-review", response_model=GitHubFileReviewResponse)
def review_github_file(
    request: GitHubFileReviewRequest,
    client: Annotated[GitHubRepositoryClient, Depends(get_github_repository_client)],
) -> GitHubFileReviewResponse:
    """读取一个受限 Python 文件，再用现有 AST 审查器分析，不执行源码。"""
    try:
        code = client.fetch_python_file(request.repository_url, request.file_path)
    except GitHubNotFoundError:
        raise HTTPException(status_code=404, detail="仓库或文件不存在或不可公开访问。") from None
    except GitHubRateLimitError:
        raise HTTPException(status_code=503, detail="GitHub 请求受到限流，请稍后重试。") from None
    except GitHubForbiddenError:
        raise HTTPException(status_code=502, detail="GitHub 拒绝访问该仓库。") from None
    except GitHubRedirectError:
        raise HTTPException(
            status_code=502, detail="GitHub 返回了重定向，请使用仓库当前的主页地址。"
        ) from None
    except GitHubUpstreamError:
        raise HTTPException(
            status_code=502, detail="GitHub 返回了无效或非预期的文件响应。"
        ) from None
    except GitHubTimeoutError:
        raise HTTPException(status_code=504, detail="GitHub 请求超时。") from None
    except GitHubNetworkError:
        raise HTTPException(status_code=502, detail="GitHub 网络请求失败。") from None
    except GitHubFileTooLargeError:
        raise HTTPException(
            status_code=422, detail="Python 文件超过大小限制，最多允许 20000 字节。"
        ) from None
    except GitHubUnsupportedFileError:
        raise HTTPException(status_code=422, detail="GitHub 文件类型或编码不受支持。") from None

    # 下载异常的转换到此结束；不掩盖审查器或响应组装中的程序错误。
    # 不使用要求非空 code 的 ReviewRequest，空文件也是合法的 Python 源码。
    issues = review_python(code)
    return GitHubFileReviewResponse(
        repository_url=request.repository_url,
        file_path=request.file_path,
        review=ReviewResponse(language="python", issue_count=len(issues), issues=issues),
    )
