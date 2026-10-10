"""ForgeMesh 控制平面（命名空间包 ``forgemesh.controlplane``）。

阶段 0 仅提供 HTTP 应用骨架与契约占位路由，不连接任何数据库：

* :mod:`forgemesh.controlplane.app` —— FastAPI 应用工厂与占位路由
  （``/healthz`` / ``/readyz`` 探针；``/api/v1/...`` 统一返回 501）。
"""

from __future__ import annotations

from .app import (
    API_PREFIX,
    CONTROL_PLANE_VERSION,
    NOT_IMPLEMENTED_CODE,
    PLACEHOLDER_ROUTES,
    HealthResponse,
    create_app,
    error_envelope,
)

__all__ = [
    "API_PREFIX",
    "CONTROL_PLANE_VERSION",
    "NOT_IMPLEMENTED_CODE",
    "PLACEHOLDER_ROUTES",
    "HealthResponse",
    "error_envelope",
    "create_app",
]
