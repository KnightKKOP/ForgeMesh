"""forgemesh.qos.basic 插件包（QoS 基础策略）。

目录使用连字符命名（``qos-basic``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供：

* ``QoSProvider`` — QoS 策略 Provider（封装 QoSManager）
* ``QoSProviderResult`` — 评估结果
"""

from __future__ import annotations

from .provider import QoSProvider, QoSProviderResult

__all__ = [
    "QoSProvider",
    "QoSProviderResult",
]
