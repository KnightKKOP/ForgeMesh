"""forgemesh.metering.basic 插件包（基础 Token 计量）。

目录使用连字符命名（``metering-basic``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供：

* ``UsageRecord`` — 追加式使用记录
* ``UsageStore`` — SQLite 使用记录存储（追加式，支持计量修正 IVT-10）
* ``UsageSummary`` — 聚合统计
* ``MeteringProvider`` — 计量 Provider（封装 UsageStore 为可插拔组件）
"""

from __future__ import annotations

from .provider import MeteringProvider
from .usage import UsageRecord, UsageStore, UsageSummary

__all__ = [
    "MeteringProvider",
    "UsageRecord",
    "UsageStore",
    "UsageSummary",
]
