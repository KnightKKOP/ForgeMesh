"""forgemesh.workflow.temporal 插件包（Temporal 工作流运行时 Provider）。

目录使用连字符命名（``workflow-temporal``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供子模块：

* ``provider`` — Temporal Workflow Provider，实现 :class:`WorkflowRuntime` 契约
"""

from __future__ import annotations

from .provider import (
    TemporalRunStatus,
    TemporalWorkflowClient,
    TemporalWorkflowProvider,
    temporal_eval_report,
)

__all__ = [
    "TemporalRunStatus",
    "TemporalWorkflowClient",
    "TemporalWorkflowProvider",
    "temporal_eval_report",
]
