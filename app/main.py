"""FastAPI 入口：排程审计 HTTP 服务。

路由：
- GET  /                 服务信息
- GET  /healthz          健康检查（容器健康探针使用）
- POST /api/v1/audit     夜间观测排程审计
"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .errors import AuditError, issue
from .schemas import (
    AuditRequest, AuditResponse, HealthOut, RejectResponse,
)
from .scheduler import run_audit

app = FastAPI(
    title="Observatory Night Schedule Audit API",
    description="天文台夜间观测排程审计：可见窗口、滤镜切换、逐帧时间线",
    version=__version__,
)


def _request_id(req: Request) -> str:
    return str(uuid.uuid4())


@app.exception_handler(AuditError)
async def audit_error_handler(request: Request, exc: AuditError) -> JSONResponse:
    body = RejectResponse(request_id=_request_id(request),
                          issues=exc.issues).model_dump(mode="json")
    return JSONResponse(status_code=422, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request,
                                   exc: RequestValidationError) -> JSONResponse:
    issues: list[AuditIssue] = []
    for err in exc.errors():
        # 将 pydantic 的 loc 元组转换为 JSON 路径：
        # ("body","sequence",0,"filter") -> "sequence[0].filter"
        segments: list[str] = []
        item_index = None
        for p in err.get("loc", []):
            if p in ("body", ""):
                continue
            if isinstance(p, int):
                item_index = p
                if segments:
                    segments[-1] = f"{segments[-1]}[{p}]"
                else:
                    segments.append(f"[{p}]")
            else:
                segments.append(str(p))
        loc = ".".join(segments) or "body"

        etype = err.get("type", "value_error")
        if etype == "extra_forbidden":
            code = "AMBIGUOUS_INPUT"
            msg = (f"存在未定义字段 {err.get('loc', ['?'])[-1]!r}，"
                   "可能是拼写错误，按歧义拒绝")
        elif etype == "missing":
            code = "INVALID_INPUT"
            msg = f"缺少必填字段: {err.get('msg', '')}"
        else:
            code = "INVALID_INPUT"
            msg = err.get("msg", "请求参数不合法")
        issues.append(issue(code, msg, loc, item_index=item_index,
                            detail={"type": etype}))
    body = RejectResponse(request_id=_request_id(request),
                          issues=issues).model_dump(mode="json")
    return JSONResponse(status_code=422, content=body)


@app.get("/", response_model=HealthOut, tags=["meta"])
async def root() -> HealthOut:
    return HealthOut(status="ok",
                     service="night-schedule-audit", version=__version__)


@app.get("/healthz", response_model=HealthOut, tags=["meta"])
async def healthz() -> HealthOut:
    return HealthOut(status="ok",
                     service="night-schedule-audit", version=__version__)


@app.post("/api/v1/audit",
          response_model=AuditResponse,
          responses={422: {"model": RejectResponse}},
          tags=["audit"])
async def audit(req: AuditRequest, request: Request) -> AuditResponse:
    return run_audit(req, request_id=_request_id(request))
