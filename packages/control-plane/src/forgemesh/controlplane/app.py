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
* **调度器**：设置 ``FORGEMESH_INVENTORY_FILE``（静态加速器清单，格式见
  :mod:`forgemesh.controlplane.scheduler.inventory`）时，控制器的协调器使用
  :class:`~forgemesh.controlplane.scheduler.FirstFitScheduler`，清单不可读或非法时
  拒绝启动；未设置时需要分配加速器的资源报告 ``Ready=False``
  （``SchedulerUnavailable``）。
* **部署**：``FORGEMESH_RUNTIME_LAUNCHER``（``fake`` / ``vllm-process`` / ``tgi-process``，
  只能与清单一起使用，见 :mod:`forgemesh.controlplane.deployment.config`）选择推理实例的启动器，配置
  非法时拒绝启动。此时协调器分配加速器、经部署工作流启动并验证实例，就绪后写入
  ``status.endpoints``；删除时停止实例、释放分配。工作流运行存放在单独的数据库文件
  （:func:`~forgemesh.controlplane.sqlite.workflow_db_path`，如 ``forgemesh.workflows.db``）
  中。启动时先结束上次运行留下、仍在运行的实例进程（进程启动器把实例记在数据库旁的文件
  中，见 :func:`~forgemesh.controlplane.deployment.record_file_path`），再恢复未完成的部署，
  然后把实例已不在运行的 ``Ready`` 服务转为 ``Degraded``（控制器随后重新部署它们），最后
  才启动控制器；控制器运行中每隔
  ``FORGEMESH_INSTANCE_CHECK_INTERVAL_SECONDS`` 秒（默认 10，不论是否选择启动器都校验）
  再核对一次实例，启动时与每次全量同步之后停止归属资源已不存在的分配所属的部署、让这些
  分配过期。关闭时依次停止控制器、工作流运行时、启动器（其实例随之停止）。
  ``POST /workflows`` 不使用这个运行时（仍返回 503）。未设置时不部署，协调器在分配之前
  即报告 ``WorkflowRuntimeUnavailable``。
* **无导入副作用**：数据库在应用启动（lifespan）时才打开，导入本模块不会
  创建文件。
* **探针**：``/healthz``（存活）恒为 ok；``/readyz``（就绪）探测权威存储可用。
* **FastAPI 可选兜底**：若运行环境未安装 FastAPI，回退到纯标准库
  ``http.server``，仅提供探针与 501 占位。

基于 Pydantic v2。
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from forgemesh.policy.engine import DefaultPolicyEngine
from forgemesh.policy.rules import AuthzRule
from forgemesh.workflowruntime.approval import ApprovalGate
from forgemesh.workflowruntime.compensate import CompensationExecutor
from forgemesh.workflowruntime.exec import WorkflowExecutor
from forgemesh.workflowruntime.runtime import LocalWorkflowRuntime
from forgemesh.workflowruntime.store import WorkflowStore
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
from .deployment import (
    DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
    RUNTIME_LAUNCHER_ENV,
    DeploymentReleaser,
    DeploymentSteps,
    check_instances,
    instance_check_interval_from_env,
    launcher_from_env,
    reclaim_orphaned_allocations,
    record_file_path,
)
from .reconciler import ProcessedKeyStore, ReconciliationController
from .scheduler import INVENTORY_FILE_ENV, FirstFitScheduler, inventory_from_env
from .sqlite import workflow_db_path
from .version import CONTROL_PLANE_VERSION

if TYPE_CHECKING:  # pragma: no cover - 仅静态类型检查使用
    from .auth import Authenticator
    from .deployment import RuntimeLauncher
    from .registry import SQLiteRegistry
    from .scheduler import Inventory

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

DB_PATH_ENV = "FORGEMESH_DB_PATH"
DEFAULT_DB_PATH = "forgemesh.db"
HOST_ENV = "FORGEMESH_HOST"
DEFAULT_HOST = "127.0.0.1"
PORT_ENV = "FORGEMESH_PORT"
DEFAULT_PORT = 8080
#: 控制器轮询资源变更的间隔（秒）。
CONTROLLER_POLL_INTERVAL_SECONDS = 0.5
#: 部署进行中时协调器查询工作流运行的间隔（秒）。
WORKFLOW_POLL_INTERVAL_SECONDS = 2.0


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


def _deployment_runtime(launcher: RuntimeLauncher, store: WorkflowStore) -> LocalWorkflowRuntime:
    steps = DeploymentSteps(launcher)
    compensation = CompensationExecutor()
    for step_id, handler in steps.compensations().items():
        compensation.registry.register(step_id, handler)
    return LocalWorkflowRuntime(
        store=store,
        executor=WorkflowExecutor(),
        compensation=compensation,
        approval=ApprovalGate(),
        handler=steps.handle,
    )


def _build_controller(
    registry: SQLiteRegistry,
    processed_store: ProcessedKeyStore,
    inventory: Inventory | None,
    runtime: LocalWorkflowRuntime | None = None,
    launcher: RuntimeLauncher | None = None,
    instance_check_interval: float = DEFAULT_INSTANCE_CHECK_INTERVAL_SECONDS,
) -> ControllerManager:
    # 没有授权规则的 DefaultPolicyEngine 拒绝一切操作。资源的创建和删除已经由 API 的
    # 认证与 scope 把关，协调只是推进这些已被接受的期望状态，因此放行 reconcile。
    policy = DefaultPolicyEngine(authz_rules=[AuthzRule(action="reconcile", effect="allow")])
    scheduler = FirstFitScheduler(registry, inventory) if inventory is not None else None
    release = (
        DeploymentReleaser(runtime, launcher)
        if runtime is not None and launcher is not None
        else None
    )
    reconciler = ReconciliationController(
        registry,
        policy,
        scheduler=scheduler,
        workflow_runtime=runtime,
        release_provider=release,
        processed_store=processed_store,
        record_dedup_keys=False,
        workflow_poll_interval=WORKFLOW_POLL_INTERVAL_SECONDS,
        launcher=launcher,
    )
    instance_check = (
        functools.partial(check_instances, registry, launcher) if launcher is not None else None
    )
    reclaim = (
        functools.partial(reclaim_orphaned_allocations, registry, release)
        if release is not None
        else None
    )
    return ControllerManager(
        registry,
        reconciler,
        poll_interval=CONTROLLER_POLL_INTERVAL_SECONDS,
        instance_check=instance_check,
        instance_check_interval=instance_check_interval,
        reclaim=reclaim,
    )


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
            reset_registry,
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
            inventory = inventory_from_env()
            if inventory is not None:
                logger.info(
                    "first-fit scheduler enabled from %s: %d node(s), %d device(s)",
                    INVENTORY_FILE_ENV,
                    len(inventory.nodes),
                    sum(len(node.devices) for node in inventory.nodes),
                )
            path = resolve_db_path(db_path)
            launcher = launcher_from_env(inventory, record_file=record_file_path(path))
            instance_check_interval = instance_check_interval_from_env()
            async with AsyncExitStack() as cleanup:
                registry = init_registry(path)
                cleanup.push_async_callback(registry.close)
                cleanup.callback(reset_registry)
                logger.info("control plane registry opened: %s", path)
                init_authenticator(auth)
                cleanup.callback(reset_authenticator)
                processed_store = ProcessedKeyStore(path)
                cleanup.push_async_callback(processed_store.close)
                runtime: LocalWorkflowRuntime | None = None
                if launcher is not None:
                    workflow_path = workflow_db_path(path)
                    workflow_store = WorkflowStore(workflow_path)
                    cleanup.push_async_callback(workflow_store.close)
                    # 运行时关闭时的补偿还会调用启动器的 stop，因此启动器在运行时之后关闭。
                    cleanup.push_async_callback(launcher.aclose)
                    runtime = _deployment_runtime(launcher, workflow_store)
                    cleanup.push_async_callback(runtime.aclose)
                    logger.info(
                        "deployments enabled from %s: %s, workflow store %s",
                        RUNTIME_LAUNCHER_ENV,
                        type(launcher).__name__,
                        workflow_path,
                    )
                    await launcher.start()
                    resumed = await runtime.recover()
                    checked = await check_instances(registry, launcher, after_restart=True)
                    logger.info(
                        "deployments recovered: %d unfinished run(s) resumed, %d service(s) "
                        "whose instance is gone or back handed to the controller",
                        len(resumed),
                        len(checked),
                    )
                controller = _build_controller(
                    registry, processed_store, inventory, runtime, launcher, instance_check_interval
                )
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
