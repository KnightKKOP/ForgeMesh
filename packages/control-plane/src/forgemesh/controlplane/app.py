"""ForgeMesh 控制平面 HTTP 应用骨架（阶段 0）。

设计要点
--------
* **仅契约、不实现**：所有 ``/api/v1/...`` 端点返回 ``501 Not Implemented``，
  并携带统一错误信封 ``{"error": {"code", "message", "correlationId"}}``，与
  ``api/openapi/openapi.yaml`` 一致。
* **不连接数据库**：阶段 0 不接入 PostgreSQL / Redis，也不假设任何外部依赖。
* **探针**：``/healthz``（存活）与 ``/readyz``（就绪）返回 :class:`HealthResponse`。
* **FastAPI 可选兜底**：若运行环境未安装 FastAPI，本模块自动回退到纯标准库
  ``http.server``，提供一个只读最小服务（探针 + 501 占位），保证骨架在无第三方
  Web 框架时仍可导入与启动。生产实现必须使用 FastAPI（或等效 ASGI 框架），
  并接入权威资源注册表（``forgemesh.core.registry``）与工作流引擎。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from pydantic import BaseModel

try:  # pragma: no cover - 取决于运行环境是否安装 FastAPI
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover - 兜底路径
    _FASTAPI_AVAILABLE = False

#: 对外 API 版本前缀（架构 13.1）。
API_PREFIX = "/api/v1"

#: 阶段 0 占位错误码。
NOT_IMPLEMENTED_CODE = "NOT_IMPLEMENTED"

#: 控制平面版本（用于探针与事件 producer.version）。
CONTROL_PLANE_VERSION = "0.0.1"


class HealthResponse(BaseModel):
    """探针响应体（与 OpenAPI ``HealthResponse`` 对齐）。"""

    status: str = "ok"
    version: str = CONTROL_PLANE_VERSION
    component: str = "control-plane"


def error_envelope(
    code: str,
    message: str,
    correlation_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造统一错误信封。

    Args:
        code: 机器可读错误码（如 ``NOT_IMPLEMENTED`` / ``CONFLICT``）。
        message: 人类可读说明。
        correlation_id: 全链路关联 ID（INV-4），可为空。
        details: 附加结构化信息，可为空。
    """
    error: Dict[str, Any] = {"code": code, "message": message}
    if correlation_id is not None:
        error["correlationId"] = correlation_id
    if details is not None:
        error["details"] = details
    return {"error": error}


#: 阶段 0 占位路由表：方法、路径、说明。真实实现将在此注册到路由器。
PLACEHOLDER_ROUTES = (
    ("GET", f"{API_PREFIX}/inference-services", "列出推理服务"),
    ("POST", f"{API_PREFIX}/inference-services", "创建推理服务"),
    ("GET", f"{API_PREFIX}/inference-services/{{name}}", "获取推理服务"),
    ("DELETE", f"{API_PREFIX}/inference-services/{{name}}", "删除推理服务"),
    ("POST", f"{API_PREFIX}/workflows", "启动工作流运行"),
    ("POST", f"{API_PREFIX}/decisions", "请求结构化决策"),
)


def _placeholder_payload(method: str, path: str) -> Dict[str, Any]:
    """生成占位响应载荷（501）。"""
    return error_envelope(
        NOT_IMPLEMENTED_CODE,
        f"阶段 0 占位：{method} {path} 尚未实现。",
    )


if _FASTAPI_AVAILABLE:

    def create_app() -> "FastAPI":
        """创建 FastAPI 应用并注册探针与占位路由。

        阶段 0 **不连接数据库**，所有资源/运行端点返回 501。
        """
        application = FastAPI(
            title="ForgeMesh Control Plane API",
            version=CONTROL_PLANE_VERSION,
            description="阶段 0 契约骨架（占位，未实现业务逻辑）。",
        )

        @application.get("/healthz", response_model=HealthResponse, tags=["probes"])
        async def healthz() -> HealthResponse:
            """存活探针。"""
            return HealthResponse(status="ok")

        @application.get("/readyz", response_model=HealthResponse, tags=["probes"])
        async def readyz() -> HealthResponse:
            """就绪探针（阶段 0 恒为 ok）。"""
            return HealthResponse(status="ok")

        for method, path, _description in PLACEHOLDER_ROUTES:

            def _make_handler(p: str = path, m: str = method):
                async def _handler() -> JSONResponse:
                    return JSONResponse(status_code=501, content=_placeholder_payload(m, p))

                return _handler

            application.add_api_route(
                path,
                _make_handler(),
                methods=[method],
                status_code=501,
                include_in_schema=False,
            )

        return application

    app = create_app()

else:  # pragma: no cover - 兜底路径：无 FastAPI 时使用标准库实现

    def create_app() -> "FastAPI":  # type: ignore[valid-type]
        """无 FastAPI 环境时的占位工厂（抛出明确错误）。"""
        raise RuntimeError(
            "未检测到 FastAPI。请安装可选依赖：pip install 'forgemesh-control-plane[api]'，"
            "或使用 run_fallback_server() 启动标准库兜底服务。"
        )

    app = None  # type: ignore[assignment]

    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _FallbackHandler(BaseHTTPRequestHandler):
        """标准库兜底处理器：仅实现探针与 501 占位。"""

        server_version = f"forgeMesh-control-plane/{CONTROL_PLANE_VERSION}"

        def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定命名
            if self.path in ("/healthz", "/readyz"):
                self._write_json(200, HealthResponse().model_dump())
                return
            self._write_json(501, _placeholder_payload("GET", self.path))

        def do_POST(self) -> None:  # noqa: N802
            self._write_json(501, _placeholder_payload("POST", self.path))

        def do_DELETE(self) -> None:  # noqa: N802
            self._write_json(501, _placeholder_payload("DELETE", self.path))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # 阶段 0 骨架：抑制默认访问日志，避免噪声。
            return

    def run_fallback_server(host: str = "0.0.0.0", port: int = 8080) -> None:
        """启动标准库兜底 HTTP 服务（阻塞）。"""
        server = HTTPServer((host, port), _FallbackHandler)
        server.serve_forever()


def main() -> None:
    """命令行入口：优先 FastAPI + uvicorn，否则回退标准库服务。"""
    if _FASTAPI_AVAILABLE:
        try:  # pragma: no cover - 依赖运行时环境
            import uvicorn

            uvicorn.run(app, host="0.0.0.0", port=8080)
            return
        except Exception:
            pass
    run_fallback_server()


__all__ = [
    "API_PREFIX",
    "NOT_IMPLEMENTED_CODE",
    "CONTROL_PLANE_VERSION",
    "PLACEHOLDER_ROUTES",
    "HealthResponse",
    "error_envelope",
    "create_app",
    "app",
    "main",
]
