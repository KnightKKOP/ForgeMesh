"""ForgeMesh 资源注册表契约（权威资源状态）。

对齐 ``docs/architecture/development-plan.md`` 5.2：注册表是**权威状态**（ADR-007），
所有写操作须在存储事务中完成。本模块仅定义抽象基类，具体实现（如
PostgreSQL 存储）在控制平面子包中提供。

**版本并发语义（乐观并发 / CAS）**：

* ``get`` 读取单个资源；不存在抛 :class:`~forgemesh.core.errors.NotFoundError`；
  跨租户访问一律视为 NotFound（不泄露存在性），由实现按 tenant_id 过滤。
* ``put(resource)`` 写入资源：

  - 传入的 ``metadata.resourceVersion`` 为 ``None`` → 视为 **Create**；若同名资源
    已存在，抛 :class:`~forgemesh.core.errors.ConflictError`。
  - ``resourceVersion`` 非 ``None`` → **Update-CAS**；与当前存储版本不一致时抛
    :class:`~forgemesh.core.errors.ConflictError`，调用方应重读后重试。
  - 成功写入后，存储层负责将 ``resourceVersion`` 递增并回填返回值。
  - **幂等**：相同 ``(uid, generation, spec_digest)`` 的重复写不产生额外副作用。

* ``list`` 按 kind/namespace/labels 过滤返回集合（强制注入租户边界）。
* ``delete`` 删除资源；不存在时抛 :class:`~forgemesh.core.errors.NotFoundError`。

兼容 Python 3.9+。
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

from .objects import Resource


class ResourceRegistry(ABC):
    """资源注册表抽象基类。

    实现方必须保证上述乐观并发与租户隔离语义；方法均为异步以适配 I/O 后端。
    """

    @abstractmethod
    async def get(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
    ) -> Resource:
        """读取单个资源。

        Args:
            kind: 资源类型标识。
            namespace: 命名空间（可为 ``None`` 表示集群级）。
            name: 资源名。
            tenant_id: 租户标识（多租户边界，跨租户访问一律视为 NotFound）。

        Raises:
            NotFoundError: 资源不存在，或调用方无权感知其存在。
        """
        raise NotImplementedError

    @abstractmethod
    async def put(
        self,
        resource: Resource,
        expected_version: str | None = None,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> Resource:
        """写入（Create 或 Update-CAS）资源，返回写入后的最新对象。

        ``expected_version`` 为 ``None`` 表示 Create，否则为 CAS Update。
        版本不匹配或唯一约束冲突时抛 :class:`ConflictError`。

        Args:
            resource: 待写入资源。
            expected_version: 期望的当前 ``resourceVersion``（乐观并发 CAS 依据）。
            actor: 操作发起者标识（审计用）。
            correlation_id: 关联追踪标识（审计/可观测性用）。
        """
        raise NotImplementedError

    @abstractmethod
    async def list(
        self,
        kind: str,
        tenant_id: str,
        namespace: str | None = None,
        labels: dict[str, str] | None = None,
        watch: bool = False,
        continue_token: str | None = None,
        limit: int = 100,
    ) -> builtins.list[Resource]:
        """按 ``kind`` + 可选 ``namespace`` / ``labels`` 过滤列出资源。

        Args:
            kind: 资源类型标识。
            tenant_id: 租户标识（多租户边界，强制注入，不可省略）。
            namespace: 命名空间过滤（可选）。
            labels: 标签选择器（可选）。
            watch: 是否以 watch 模式持续推送变更（默认 ``False``）。
            continue_token: 分页游标（上一页返回的 opaque token）。
            limit: 单页上限（默认 100）。
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(
        self,
        kind: str,
        namespace: str | None,
        name: str,
        tenant_id: str,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """删除资源。

        Args:
            kind: 资源类型标识。
            namespace: 命名空间（可为 ``None``）。
            name: 资源名。
            tenant_id: 租户标识（多租户边界）。
            actor: 操作发起者标识（审计用）。
            correlation_id: 关联追踪标识（审计/可观测性用）。

        Raises:
            NotFoundError: 目标资源不存在。
        """
        raise NotImplementedError


__all__ = ["ResourceRegistry"]
