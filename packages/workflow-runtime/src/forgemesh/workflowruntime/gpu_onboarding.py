"""GPU 上线端到端工作流定义（对齐 development-plan WP-3.5）。

定义 GPU 上线的 6 步闭环：

    discover → validate → prepare → deploy → verify → commit

各步骤职责：

* **discover** — GPU 发现（``gpu.discovery``）：扫描节点上的可用 GPU。
* **validate** — 驱动/CUDA 预检（``gpu.preflight``）：校验驱动版本与 CUDA 可用性。
* **prepare**  — 模型准备（``model.prepare``）：拉取模型并校验摘要。
* **deploy**   — 部署推理服务（``inference.deploy``）：启动推理实例并绑定端口。
* **verify**   — 服务验收（``inference.verify``）：健康检查与推理冒烟测试。
* **commit**   — 提交上线（``resource.commit``）：登记资源所有权并置 Ready。

本模块仅定义工作流结构，不执行真实副作用；执行由
:class:`forgemesh.workflowruntime.exec.WorkflowExecutor` 驱动。

兼容 Python 3.9+。
"""

from __future__ import annotations

from typing import Any

from forgemesh.workflow.runtime import Step, WorkflowDefinition


class GPUOnboardingWorkflow:
    """GPU 上线工作流定义。

    定义从 GPU 发现到提交上线的完整 6 步闭环。步骤间以线性依赖串联，
    构成无环 DAG，任一步骤失败将终止后续步骤。
    """

    #: 步骤名称列表（按执行顺序，用于测试和文档）。
    ONBOARDING_STEPS: list[str] = [
        "discover",
        "validate",
        "prepare",
        "deploy",
        "verify",
        "commit",
    ]

    def build_definition(self) -> WorkflowDefinition:
        """构建 6 步 GPU 上线工作流定义。

        步骤以线性依赖串联：每一步依赖前一步，构成链式 DAG。

        Returns:
            含 6 个步骤的 :class:`WorkflowDefinition`。
        """
        return WorkflowDefinition(
            name="gpu-onboarding",
            timeout_seconds=1800,
            steps=[
                Step(id="discover", uses="gpu.discovery"),
                Step(id="validate", uses="gpu.preflight", needs=["discover"]),
                Step(id="prepare", uses="model.prepare", needs=["validate"]),
                Step(id="deploy", uses="inference.deploy", needs=["prepare"]),
                Step(id="verify", uses="inference.verify", needs=["deploy"]),
                Step(id="commit", uses="resource.commit", needs=["verify"]),
            ],
        )

    def build_inputs(
        self,
        node_id: str,
        model_uri: str,
        model_digest: str,
        gpu_ids: list[str],
        port: int,
    ) -> dict[str, Any]:
        """构建工作流输入。

        Args:
            node_id: 目标节点 ID。
            model_uri: 模型资源 URI。
            model_digest: 模型摘要（用于完整性校验）。
            gpu_ids: 目标 GPU ID 列表。
            port: 推理服务监听端口。

        Returns:
            工作流输入字典，供各步骤 handler 读取。
        """
        return {
            "node_id": node_id,
            "model_uri": model_uri,
            "model_digest": model_digest,
            "gpu_ids": gpu_ids,
            "port": port,
        }


__all__ = [
    "GPUOnboardingWorkflow",
]
