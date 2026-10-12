"""ForgeMesh 控制平面 HTTP 应用。

设计要点
--------
* **真实路由**：挂载 :func:`forgemesh.controlplane.api.create_api_router`
  （资源 CRUD / 工作流 / 决策），错误统一使用
  ``{"error": {"code", "message", "correlationId"}}`` 信封，与
  ``api/openapi/openapi.yaml`` 一致。
* **配置**：权威存储路径取自环境变量 ``FORGEMESH_DB_PATH``（默认
  ``forgemesh.db``）；监听地址取自 ``FORGEMESH_HOST`` / ``FORGEMESH_PORT``
  （默认 ``127.0.0.1:8080``）。
* **认证**：启动时按 ``FORGEMESH_AUTH_MODE`` 构造认证器（默认 ``apikey``，需要
  ``FORGEMESH_API_KEYS_FILE``），配置缺失或非法时拒绝启动；``dev`` 模式只允许
  监听回环地址（见 :mod:`forgemesh.controlplane.auth`）。
* **控制器**：lifespan 中启动 :class:`~forgemesh.controlplane.controller.ControllerManager`，
  自动协调 ``InferenceService`` 并回写 status；去重键与资源存放在同一个数据库
  文件中。控制器是单写者（ADR-013），本应用只能以单进程运行（不要使用
  ``uvicorn --workers``）。
* **无导入副作用**：数据库在应用启动（lifespan）时才打开，导入本模块不会
  创建文件。
* **探针**：``/healthz``（存活）恒为 ok；``/readyz``（就绪）探测权威存储可用。
* **FastAPI 可选兜底**：若运行环境未安装 FastAPI，回退到纯标准库
  ``http.server``，仅提供探针与 501 占位。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from forgemesh.policy.engine import DefaultPolicyEngine
from forgemesh.policy.rules import AuthzRule
from pydantic import BaseModel

from .auth import (
    AUTH_MODE_DEV,
    AUTH_MODE_ENV,
    AuthConfigError,
    HeaderAuthenticator,
    auth_mode_from_env,
    authenticator_from_env,
)
from .controller import ControllerManager
from .reconciler import ProcessedKeyStore, ReconciliationController

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from .auth import Authenticator
    from .registry import SQLiteRegistry

try:  # pragma: no cover - 取决于运行环境是否安装 FastAPI
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except Exception:  # pragma: no cover - 兜底路径
    _FASTAPI_AVAILABLE = False

logger = logging.getLogger(__name__)

#: 对外 API 版本前缀（架构 13.1）。
API_PREFIX = "/api/v1"

#: 兜底服务（无 FastAPI）的占位错误码。
NOT_IMPLEMENTED_CODE = "NOT_IMPLEMENTED"

#: 控制平面版本（用于探针与事件 producer.version）。
CONTROL_PLANE_VERSION = "0.0.1"

DB_PATH_ENV = "FORGEMESH_DB_PATH"
DEFAULT_DB_PATH = "forgemesh.db"
HOST_ENV = "FORGEMESH_HOST"
DEFAULT_HOST = "127.0.0.1"
PORT_ENV = "FORGEMESH_PORT"
DEFAULT_PORT = 8080


class HealthResponse(BaseModel):
    """探针响应体（与 OpenAPI ``HealthResponse`` 对齐）。"""

    status: str = "ok"
    version: str = CONTROL_PLANE_VERSION
    component: str = "control-plane"


def error_envelope(
    code: str,
    message: str,
    correlation_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一错误信封。

    Args:
        code: 机器可读错误码（如 ``NOT_IMPLEMENTED`` / ``CONFLICT``）。
        message: 人类可读说明。
        correlation_id: 全链路关联 ID（INV-4），可为空。
        details: 附加结构化信息，可为空。
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if correlation_id is not None:
        error["correlationId"] = correlation_id
    if details is not None:
        error["details"] = details
    return {"error": error}


def _placeholder_payload(method: str, path: str) -> dict[str, Any]:
    """生成兜底服务的占位响应载荷（501）。"""
    return error_envelope(
        NOT_IMPLEMENTED_CODE,
        f"未安装 FastAPI：{method} {path} 不可用。",
    )


def resolve_db_path(db_path: str | None = None) -> str:
    """显式参数优先，其次环境变量 ``FORGEMESH_DB_PATH``，最后默认值。"""
    return db_path or os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH


def _build_controller(
    registry: SQLiteRegistry, processed_store: ProcessedKeyStore
) -> ControllerManager:
    # 没有授权规则的 DefaultPolicyEngine 拒绝一切操作。资源的创建和删除已经由 API 的
    # 认证与 scope 把关，协调只是推进这些已被接受的期望状态，因此放行 reconcile。
    policy = DefaultPolicyEngine(authz_rules=[AuthzRule(action="reconcile", effect="allow")])
    reconciler = ReconciliationController(
        registry, policy, processed_store=processed_store, record_dedup_keys=False
    )
    return ControllerManager(registry, reconciler)


if _FASTAPI_AVAILABLE:

    def create_app(
        db_path: str | None = None, authenticator: Authenticator | None = None
    ) -> FastAPI:
        """创建 FastAPI 应用：探针 + 真实 API 路由；认证器与注册中心在启动时初始化。

        Args:
            db_path: SQLite 权威存储路径；为空时按 :func:`resolve_db_path` 解析。
            authenticator: 认证器；为空时在启动时按环境变量构造
                （见 :func:`forgemesh.controlplane.auth.authenticator_from_env`），
                配置缺失或非法则启动失败。
        """
        # 延迟导入：api.routes 反向依赖本模块的 error_envelope。
        from .api import create_api_router
        from .api.dependencies import (
            get_registry,
            init_authenticator,
            init_registry,
            reset_authenticator,
        )
        from .api.errors import install_exception_handlers

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            auth = authenticator if authenticator is not None else authenticator_from_env()
            if isinstance(auth, HeaderAuthenticator):
                logger.warning(
                    "%s=%s: tenant identity is taken from the X-Tenant-Id header without "
                    "verification; use this mode for local development only",
                    AUTH_MODE_ENV,
                    AUTH_MODE_DEV,
                )
            path = resolve_db_path(db_path)
            async with AsyncExitStack() as cleanup:
                registry = init_registry(path)
                cleanup.push_async_callback(registry.close)
                logger.info("control plane registry opened: %s", path)
                init_authenticator(auth)
                cleanup.callback(reset_authenticator)
                processed_store = ProcessedKeyStore(path)
                cleanup.push_async_callback(processed_store.close)
                controller = _build_controller(registry, processed_store)
                cleanup.push_async_callback(controller.stop)
                await controller.start()
                yield

        application = FastAPI(
            title="ForgeMesh Control Plane API",
            version=CONTROL_PLANE_VERSION,
            lifespan=lifespan,
        )

        @application.get("/healthz", response_model=HealthResponse, tags=["probes"])
        async def healthz() -> HealthResponse:
            """存活探针。"""
            return HealthResponse(status="ok")

        @application.get("/readyz", response_model=HealthResponse, tags=["probes"])
        async def readyz() -> Any:
            """就绪探针：权威存储不可用时返回 503。"""
            try:
                await get_registry().ping()
            except Exception as exc:  # noqa: BLE001 - 任何存储故障都视为未就绪
                return JSONResponse(
                    status_code=503,
                    content=error_envelope("NOT_READY", f"registry unavailable: {exc}"),
                )
            return HealthResponse(status="ok")

        install_exception_handlers(application)
        application.include_router(create_api_router())
        return application

    app = create_app()

else:  # pragma: no cover - 兜底路径：无 FastAPI 时使用标准库实现

    def create_app(  # type: ignore[valid-type]
        db_path: str | None = None, authenticator: Authenticator | None = None
    ) -> FastAPI:
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

        def _write_json(self, status: int, payload: dict[str, Any]) -> None:
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
            return

    def run_fallback_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        """启动标准库兜底 HTTP 服务（阻塞）。"""
        server = HTTPServer((host, port), _FallbackHandler)
        server.serve_forever()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> None:
    """命令行入口：监听地址取自 ``FORGEMESH_HOST`` / ``FORGEMESH_PORT``。

    ``FORGEMESH_AUTH_MODE=dev`` 时只允许监听回环地址，否则拒绝启动。
    """
    host = os.environ.get(HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(PORT_ENV, str(DEFAULT_PORT)))
    try:
        mode = auth_mode_from_env()
    except AuthConfigError as exc:
        raise SystemExit(str(exc)) from exc
    if mode == AUTH_MODE_DEV and not _is_loopback(host):
        raise SystemExit(
            f"{AUTH_MODE_ENV}={AUTH_MODE_DEV} trusts the X-Tenant-Id header and may only listen "
            f"on a loopback address, but {HOST_ENV}={host}"
        )
    if _FASTAPI_AVAILABLE:  # pragma: no cover - 依赖运行时环境
        import uvicorn

        uvicorn.run(app, host=host, port=port)
        return
    run_fallback_server(host, port)  # pragma: no cover


__all__ = [
    "API_PREFIX",
    "NOT_IMPLEMENTED_CODE",
    "CONTROL_PLANE_VERSION",
    "DB_PATH_ENV",
    "HealthResponse",
    "error_envelope",
    "resolve_db_path",
    "create_app",
    "app",
    "main",
]
