"""forgemesh.runtime.vllm 插件包（vLLM 推理运行时 Provider）。

目录使用连字符命名（``runtime-vllm``）以符合 manifest 约定；Python 导入
通过 ``importlib`` 从文件路径加载，详见 ``tests/unit/plugins/conftest.py``。

提供四个子模块：

* ``model`` — 模型拉取与制品摘要校验
* ``deploy`` — vLLM 实例部署与路由注册
* ``verify`` — 服务验收（冒烟推理、延迟与错误率）
* ``release`` — 资源回收（停止实例、释放租约、分配核验）
"""

from __future__ import annotations

from .deploy import DeployConfig, InstanceStatus, VLLMDeployer, VLLMInstance
from .model import ModelArtifact, ModelPreparer
from .release import ReleaseResult, ResourceReleaser
from .verify import ServiceVerifier, VerifyConfig, VerifyResult

__all__ = [
    "DeployConfig",
    "InstanceStatus",
    "ModelArtifact",
    "ModelPreparer",
    "ReleaseResult",
    "ResourceReleaser",
    "ServiceVerifier",
    "VLLMDeployer",
    "VLLMInstance",
    "VerifyConfig",
    "VerifyResult",
]
