"""forgemesh.gpu.nvidia 插件包（NVIDIA GPU 硬件 Provider）。

目录使用连字符命名（``gpu-nvidia``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供三个子模块：

* ``discovery`` — GPU 发现与能力探测
* ``preflight`` — 驱动 / CUDA / 运行时预检
* ``health`` — 周期性设备健康检查
"""

from __future__ import annotations

from .discovery import DiscoveryResult, GPUDevice, GPUDiscoverer
from .health import DEGRADED, HEALTHY, UNHEALTHY, GPUHealthChecker, GPUHealthStatus
from .preflight import PreflightCheck, PreflightResult, PreflightValidator

__all__ = [
    "GPUDevice",
    "DiscoveryResult",
    "GPUDiscoverer",
    "PreflightCheck",
    "PreflightResult",
    "PreflightValidator",
    "GPUHealthStatus",
    "GPUHealthChecker",
    "HEALTHY",
    "DEGRADED",
    "UNHEALTHY",
]
