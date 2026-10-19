"""定义 API 接收和返回的数据格式。"""

from typing import Literal

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    """只接收 Python 源码，不把其他类型自动转换成字符串。"""

    code: str = Field(strict=True, min_length=1, max_length=20_000)


class ReviewIssue(BaseModel):
    """一条审查发现，行号从 1 开始。"""

    rule_id: Literal["PY_EVAL", "PY_PRINT", "PY_BARE_EXCEPT", "PY_SYNTAX"]
    severity: Literal["error", "warning", "info"]
    line: int = Field(ge=1)
    message: str


class ReviewResponse(BaseModel):
    """Python 源码的审查结果。"""

    language: Literal["python"]
    issue_count: int = Field(ge=0)
    issues: list[ReviewIssue]
