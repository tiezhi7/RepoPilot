"""FastAPI 入口：接收请求，调用审查逻辑，再返回结构化结果。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .reviewer import review_python
from .schemas import ReviewRequest, ReviewResponse

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
