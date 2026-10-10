"""ForgeMesh 插件清单与生命周期（对齐 development-plan 5.7 / 6.3 / ADR-006）。

设计要点：

* **安装状态与健康状态分离**：:class:`PluginLifecycleState` 只描述治理生命周期
  （发现→校验→安装→启用/禁用→卸载）；运行健康（Healthy/Degraded/Unhealthy）
  由独立健康检查表达，二者不得复用同一字段。
* **最小权限**：``permissions`` 声明插件所需的最小权限集合，供安装时校验与运行时限制。
* **隔离分级**：隔离模式按信任级别由安装器裁决（ADR-006），Manifest 可声明。
* **供应链**：``artifact_digest`` / ``signature`` 支持制品摘要与签名校验。

非法生命周期转移抛出 :class:`forgemesh.core.errors.InvalidStateTransition`；
若核心包未就绪，则回退到本地同名兜底异常类。

兼容 Python 3.9+。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Set, Union

from pydantic import BaseModel, ConfigDict, Field

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import InvalidStateTransition
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class InvalidStateTransition(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的非法状态转移异常。"""

        def __init__(self, message: str = "", details: dict = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


class PluginCategory(str, Enum):
    """插件类别。"""

    PROVIDER = "provider"
    HARDWARE = "hardware"
    RUNTIME = "runtime"
    CAPABILITY = "capability"
    POLICY = "policy"
    OBSERVABILITY = "observability"


class PluginLifecycleState(str, Enum):
    """插件治理生命周期状态（6 态）。

    终态：``UNINSTALLED``。
    """

    DISCOVERED = "Discovered"
    VALIDATING = "Validating"
    INSTALLED = "Installed"
    ENABLED = "Enabled"
    DISABLED = "Disabled"
    UNINSTALLED = "Uninstalled"


# 合法生命周期转移（由插件管理器校验，插件不可自定义）。
PLUGIN_TRANSITIONS: Dict[PluginLifecycleState, Set[PluginLifecycleState]] = {
    PluginLifecycleState.DISCOVERED: {
        PluginLifecycleState.VALIDATING,
    },
    PluginLifecycleState.VALIDATING: {
        PluginLifecycleState.INSTALLED,
        PluginLifecycleState.UNINSTALLED,  # 校验失败
    },
    PluginLifecycleState.INSTALLED: {
        PluginLifecycleState.ENABLED,
        PluginLifecycleState.DISABLED,
        PluginLifecycleState.UNINSTALLED,
    },
    PluginLifecycleState.ENABLED: {
        PluginLifecycleState.DISABLED,
        PluginLifecycleState.UNINSTALLED,
    },
    PluginLifecycleState.DISABLED: {
        PluginLifecycleState.ENABLED,
        PluginLifecycleState.UNINSTALLED,
    },
    # 终态：无出边
    PluginLifecycleState.UNINSTALLED: set(),
}


class PluginMeta(BaseModel):
    """插件元数据。

    Attributes:
        id: 插件唯一标识（如 ``forgemesh.runtime.vllm``）。
        version: 插件版本（语义化版本）。
    """

    id: str
    version: str


class PluginSpec(BaseModel):
    """插件规格（对齐 development-plan 6.3 PluginManifest）。

    Attributes:
        category: 插件类别。
        compatibility: 兼容性声明（如 ``{"coreApi": "v1alpha1"}``）。
        provides: 提供的能力标识。
        requires: 依赖的能力标识。
        permissions: 所需最小权限集合。
        isolation: 隔离级别（in-process/restricted-rpc/sandbox，ADR-006）。
        entrypoint: 入口点标识。
        artifactDigest: 制品摘要（如 sha256:...），供应链安全。
        signature: 制品签名（如 cosign:...），供应链安全。
        configuration: 配置 schema 引用/默认值。
        healthCheck: 健康检查配置（如 ``{"mode": "rpc", "timeoutMs": 2000}``，
            值可为字符串或整数等混合类型）。
    """

    model_config = ConfigDict(extra="forbid")

    category: PluginCategory
    compatibility: Dict[str, str] = Field(default_factory=dict)
    provides: List[str] = Field(default_factory=list)
    requires: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    isolation: Optional[str] = Field(default=None, description="隔离级别（in-process/restricted-rpc/sandbox，ADR-006）")
    entrypoint: Optional[str] = Field(default=None, description="入口点标识")
    artifactDigest: Optional[str] = Field(default=None, description="制品摘要（如 sha256:...），供应链安全")
    signature: Optional[str] = Field(default=None, description="制品签名（如 cosign:...），供应链安全")
    configuration: Dict[str, Any] = Field(default_factory=dict)
    healthCheck: Dict[str, Any] = Field(default_factory=dict)


class PluginManifest(BaseModel):
    """插件清单（声明式、随插件制品分发）。

    Attributes:
        apiVersion: 清单 API 版本。
        kind: 固定为 ``PluginManifest``。
        metadata: 插件元数据。
        spec: 插件规格。
    """

    apiVersion: str = "forgemesh.io/v1alpha1"
    kind: str = "PluginManifest"
    metadata: PluginMeta
    spec: PluginSpec


def _coerce(state: Union[PluginLifecycleState, str]) -> PluginLifecycleState:
    """将输入规范化为 :class:`PluginLifecycleState`；未知取值抛异常。"""
    if isinstance(state, PluginLifecycleState):
        return state
    try:
        return PluginLifecycleState(state)
    except ValueError as exc:
        raise InvalidStateTransition(f"unknown plugin lifecycle state: {state!r}") from exc


def validate_plugin_transition(
    cur: Union[PluginLifecycleState, str],
    to: Union[PluginLifecycleState, str],
) -> None:
    """校验插件生命周期状态转移是否合法。

    Args:
        cur: 当前生命周期状态。
        to: 目标生命周期状态。

    Raises:
        InvalidStateTransition: 当 ``cur -> to`` 不在 :data:`PLUGIN_TRANSITIONS` 中，
            或状态取值未知时抛出。
    """
    cur_s = _coerce(cur)
    to_s = _coerce(to)
    allowed = PLUGIN_TRANSITIONS.get(cur_s, set())
    if to_s not in allowed:
        raise InvalidStateTransition(
            f"illegal plugin transition: {cur_s.value} -> {to_s.value}"
        )


__all__ = [
    "PluginCategory",
    "PluginMeta",
    "PluginSpec",
    "PluginManifest",
    "PluginLifecycleState",
    "PLUGIN_TRANSITIONS",
    "validate_plugin_transition",
]
