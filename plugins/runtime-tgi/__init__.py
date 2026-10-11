"""forgemesh.runtime.tgi 插件包（TGI 推理运行时 Provider）。

目录使用连字符命名（``runtime-tgi``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供子模块：

* ``provider`` — TGI (Text Generation Inference) Runtime Provider，
  实现推理 Runtime 契约（deploy → verify → release 闭环）
"""

from __future__ import annotations

from .provider import (
    HealthStatus,
    InstanceStatus,
    ResourceAllocation,
    TgiDeployConfig,
    TgiInstance,
    TgiRuntimeProvider,
    VerifyResult,
)

__all__ = [
    "HealthStatus",
    "InstanceStatus",
    "ResourceAllocation",
    "TgiDeployConfig",
    "TgiInstance",
    "TgiRuntimeProvider",
    "VerifyResult",
]
