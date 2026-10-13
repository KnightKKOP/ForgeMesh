"""ForgeMesh 插件清单与生命周期（对齐 development-plan 5.7 / 6.3 / ADR-006）。

设计要点：

* **安装状态与健康状态分离**：:class:`PluginLifecycleState` 只描述治理生命周期
  （发现→校验→安装→启用/禁用→卸载）；运行健康（Healthy/Degraded/Unhealthy）
  由独立健康检查表达，二者不得复用同一字段。
* **权限申请**：``permissions`` 是插件申请的权限，取值限于
  :class:`~forgemesh.plugin.permissions.Permission` 词表且不得重复；清单不能给自己授权，
  实际授予由管理员决定。
* **隔离**：插件都在宿主进程内运行，``isolation`` 只能声明 ``in-process``（未设置同样按
  进程内运行）；``restricted-rpc`` / ``sandbox`` 需要真实的进程边界（ADR-006），实现之前
  清单不得声明。
* **入口点**：``entrypoint`` 形如 ``<模块>:<类>``，模块路径相对于插件包（插件目录须含
  ``__init__.py``），由 :func:`forgemesh.plugin.loader.load_entrypoint` 加载。
* **供应链**：签名是随制品分发的独立文档（:mod:`forgemesh.plugin.signing`），由插件管理器
  在注册时校验；清单中的 ``artifactDigest`` / ``signature`` 字段已弃用，不参与任何校验，
  计划在 2.0.0 移除。

非法生命周期转移抛出 :class:`forgemesh.core.errors.InvalidStateTransition`；
若核心包未就绪，则回退到本地同名兜底异常类。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from forgemesh.plugin.permissions import Permission
from pydantic import BaseModel, ConfigDict, Field, field_validator

try:  # 优先复用核心包统一异常体系
    from forgemesh.core.errors import InvalidStateTransition
except ImportError:  # pragma: no cover - 仅在核心包未就绪时触发

    class InvalidStateTransition(Exception):  # type: ignore[no-redef]
        """兜底：核心包未就绪时的非法状态转移异常。"""

        def __init__(self, message: str = "", details: dict = None) -> None:
            super().__init__(message)
            self.message = message
            self.details = details


class PluginCategory(StrEnum):
    """插件类别。"""

    PROVIDER = "provider"
    HARDWARE = "hardware"
    RUNTIME = "runtime"
    CAPABILITY = "capability"
    POLICY = "policy"
    OBSERVABILITY = "observability"


class PluginLifecycleState(StrEnum):
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
PLUGIN_TRANSITIONS: dict[PluginLifecycleState, set[PluginLifecycleState]] = {
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

#: 入口点格式 ``<模块>:<类>``：模块是相对于插件包的点分路径，类是单个标识符。
#: ``api/schemas/plugin-manifest.schema.json`` 使用同一正则。
ENTRYPOINT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"

#: 清单可声明的隔离级别。宿主没有为插件建立任何进程边界，所以只有 ``in-process`` 名副其实。
SUPPORTED_ISOLATION: tuple[str, ...] = ("in-process",)


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
        permissions: 申请的权限（:class:`Permission` 词表，不得重复）；实际授予由管理员决定。
        isolation: 隔离级别，目前只能是 ``in-process``（未设置同样按进程内运行）。
        entrypoint: 入口点 ``<模块>:<类>``（如 ``provider:RulesProvider``），模块路径相对于
            插件包。
        artifactDigest: 已弃用，不参与任何校验（见下方弃用说明）。
        signature: 已弃用，不参与任何校验（见下方弃用说明）。
        configuration: 配置 schema 引用/默认值。
        healthCheck: 健康检查配置（如 ``{"mode": "rpc", "timeoutMs": 2000}``，
            值可为字符串或整数等混合类型）。

    .. deprecated:: 1.1.0
       ``artifactDigest`` / ``signature`` 不参与任何校验，计划在 2.0.0 移除。制品摘要与签名
       记录在随制品分发的独立签名文档中（:mod:`forgemesh.plugin.signing`），用
       ``python -m forgemesh.plugin.signtool sign`` 生成。
    """

    model_config = ConfigDict(extra="forbid")

    category: PluginCategory
    compatibility: dict[str, str] = Field(default_factory=dict)
    provides: list[str] = Field(default_factory=list)
    requires: list[str] = Field(default_factory=list)
    permissions: list[Permission] = Field(default_factory=list)
    isolation: str | None = Field(default=None, description="隔离级别，目前只能是 in-process")
    entrypoint: str | None = Field(
        default=None, description="入口点 <模块>:<类>，模块路径相对于插件包"
    )
    artifactDigest: str | None = Field(
        default=None, description="已弃用，不参与任何校验，计划在 2.0.0 移除"
    )
    signature: str | None = Field(
        default=None, description="已弃用，不参与任何校验，计划在 2.0.0 移除"
    )
    configuration: dict[str, Any] = Field(default_factory=dict)
    healthCheck: dict[str, Any] = Field(default_factory=dict)

    @field_validator("permissions")
    @classmethod
    def _unique_permissions(cls, value: list[Permission]) -> list[Permission]:
        duplicates = sorted({p.value for p in value if value.count(p) > 1})
        if duplicates:
            raise ValueError(f"duplicate permissions: {duplicates}")
        return value

    @field_validator("isolation")
    @classmethod
    def _supported_isolation(cls, value: str | None) -> str | None:
        if value is not None and value not in SUPPORTED_ISOLATION:
            raise ValueError(
                f"isolation {value!r} is not supported: plugins run inside the host process "
                "without a process boundary, declare 'in-process'"
            )
        return value

    @field_validator("entrypoint")
    @classmethod
    def _entrypoint_format(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(ENTRYPOINT_PATTERN, value) is None:
            raise ValueError(
                f"entrypoint must be '<module>:<Class>' relative to the plugin package, got {value!r}"
            )
        return value


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


def _coerce(state: PluginLifecycleState | str) -> PluginLifecycleState:
    """将输入规范化为 :class:`PluginLifecycleState`；未知取值抛异常。"""
    if isinstance(state, PluginLifecycleState):
        return state
    try:
        return PluginLifecycleState(state)
    except ValueError as exc:
        raise InvalidStateTransition(f"unknown plugin lifecycle state: {state!r}") from exc


def validate_plugin_transition(
    cur: PluginLifecycleState | str,
    to: PluginLifecycleState | str,
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
        raise InvalidStateTransition(f"illegal plugin transition: {cur_s.value} -> {to_s.value}")


__all__ = [
    "ENTRYPOINT_PATTERN",
    "PluginCategory",
    "PluginMeta",
    "PluginSpec",
    "PluginManifest",
    "PluginLifecycleState",
    "PLUGIN_TRANSITIONS",
    "SUPPORTED_ISOLATION",
    "validate_plugin_transition",
]
