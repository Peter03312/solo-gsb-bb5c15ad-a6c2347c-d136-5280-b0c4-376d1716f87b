"""审计错误模型。

所有可判定的业务/输入问题统一收敛为 AuditIssue；出现任一问题时整批拒绝
（HTTP 422），并在响应中列出全部问题及其定位，便于调用方一次性修正。
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AuditIssue(BaseModel):
    code: str = Field(..., description="错误码，见 README 错误码表")
    message: str
    location: str = Field(
        ..., description="JSON 路径定位，例如 sequence[1].filter")
    item_index: Optional[int] = Field(
        None, description="涉事曝光序列项下标（0 起）")
    target: Optional[str] = Field(None, description="涉事目标名称")
    detail: Optional[dict[str, Any]] = None


class AuditError(Exception):
    """携带一批审计问题的异常，由 HTTP 层转换为 422 响应。"""

    def __init__(self, issues: list[AuditIssue]):
        self.issues = issues
        super().__init__(f"{len(issues)} issue(s) rejected")


def issue(code: str, message: str, location: str, *,
          item_index: int | None = None, target: str | None = None,
          detail: dict[str, Any] | None = None) -> AuditIssue:
    return AuditIssue(
        code=code, message=message, location=location,
        item_index=item_index, target=target, detail=detail,
    )
