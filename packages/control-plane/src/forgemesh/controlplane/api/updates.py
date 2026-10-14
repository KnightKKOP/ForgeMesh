"""资源更新（``PUT`` / ``PATCH``）：JSON Merge Patch、ETag / If-Match 与更新文档校验。

更新文档是 ``PUT`` 的请求体，或把 ``PATCH`` 的补丁（RFC 7386）合并到当前资源的 JSON
表示（与 ``GET`` 的响应相同）之后的结果，两者按同一规则解释：

* 顶层只允许 :data:`RESOURCE_FIELDS`；``status`` / ``conditions`` / ``finalizers`` 由控制
  平面维护，请求中的值被忽略；
* ``kind``、``apiVersion``、``metadata.name``、``metadata.namespace`` 与租户不可修改：
  给出时必须与当前资源一致（名称与命名空间即路径与查询参数），否则 400；
* ``metadata.uid`` / ``metadata.resourceVersion`` 给出时是前置条件，与当前值不同返回
  409；``If-Match`` 与当前 ETag 不匹配同样返回 409；
* ``generation`` / ``observedGeneration`` 由服务端维护，请求中的值被忽略；
* ``spec``、``metadata.labels`` 与 ``metadata.annotations`` 整体替换：文档中没有 labels /
  annotations 即清空。

PATCH 合并的基础是当前资源，没有改动的字段沿用当前值，因此只有补丁里给出的
``resourceVersion`` / ``uid`` 才构成前置条件。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from forgemesh.core.objects import Resource
from forgemesh.resource.inference import InferenceService
from pydantic import ValidationError

from ..registry import ConflictReason
from .errors import ApiError, validation_details

#: 资源文档允许的顶层字段。
RESOURCE_FIELDS = frozenset(
    {"apiVersion", "kind", "metadata", "spec", "status", "conditions", "finalizers"}
)

#: 由服务端维护的元数据字段：创建时一律忽略；更新时 uid / resourceVersion 作为前置条件，
#: 租户须与凭证一致，其余忽略。已验证凭证中的租户是权威来源，防止跨租户伪造。
SERVER_OWNED_METADATA = (
    "tenant_id",
    "tenantId",
    "uid",
    "resourceVersion",
    "generation",
    "observedGeneration",
    "observed_generation",
)

#: ``PATCH`` 接受的请求体媒体类型（RFC 7386）。
MERGE_PATCH_MEDIA_TYPE = "application/merge-patch+json"

#: ``If-Match`` 列表中的一个元素：``*`` 或（可带 ``W/`` 前缀的）实体标签（RFC 9110 8.8.3）；
#: 列表语法允许空元素（RFC 9110 5.6.1）。
_IF_MATCH_ELEMENT = re.compile(r'[ \t]*(?:(\*)|(W/)?("[\x21\x23-\x7e\x80-\xff]*"))?[ \t]*(?:,|$)')


def unknown_fields(document: dict[str, Any]) -> list[str]:
    """文档中 :data:`RESOURCE_FIELDS` 以外的顶层字段（排序后返回）。"""
    return sorted(set(document) - RESOURCE_FIELDS)


def apply_merge_patch(target: Any, patch: Any) -> Any:
    """按 RFC 7386 把 ``patch`` 合并到 ``target``，返回新值（不修改入参）。

    补丁中的 ``null`` 删除对应字段；对象逐字段递归合并；其余值（含数组）整体替换。
    """
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    merged = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = apply_merge_patch(merged.get(key), value)
    return merged


def etag_of(resource: Resource) -> str:
    """资源当前版本的强 ETag（带引号的不透明字符串）。

    同名资源删除后重建时 ``resourceVersion`` 从 0 重新计数，所以 ETag 同时包含 uid：
    不同的资源实例不会得到同一个 ETag。
    """
    meta = resource.metadata
    if meta.uid is None:
        return f'"{meta.resourceVersion}"'
    return f'"{meta.uid}.{meta.resourceVersion}"'


@dataclass(frozen=True)
class IfMatch:
    """解析后的 ``If-Match`` 请求头。

    Attributes:
        any: 请求头为 ``*``：资源存在即满足。
        tags: 强实体标签（含引号）；弱标签按强比较永不匹配，不收录。
    """

    any: bool
    tags: frozenset[str]

    def matches(self, etag: str) -> bool:
        """``etag`` 是否满足该前置条件（强比较）。"""
        return self.any or etag in self.tags


def parse_if_match(values: Sequence[str] | None) -> IfMatch | None:
    """解析 ``If-Match``（可出现多次，各行等价于逗号连接）；没有该请求头时返回 ``None``。

    Raises:
        ApiError: 请求头格式错误（400）。
    """
    if not values:
        return None
    any_tag = False
    tags: set[str] = set()
    elements = 0
    for value in values:
        position = 0
        while position < len(value):
            match = _IF_MATCH_ELEMENT.match(value, position)
            if match is None or match.end() == position:
                raise ApiError(400, "BAD_REQUEST", f"malformed If-Match header: {value!r}")
            star, weak, tag = match.groups()
            if star or tag:
                elements += 1
            if star:
                any_tag = True
            elif tag and not weak:
                tags.add(tag)
            position = match.end()
    if not elements:
        raise ApiError(400, "BAD_REQUEST", "If-Match must list an entity tag or '*'")
    return IfMatch(any=any_tag, tags=frozenset(tags))


@dataclass(frozen=True)
class DesiredState:
    """更新文档中可写的部分（已按模型校验并规范化）。"""

    spec: dict[str, Any]
    labels: dict[str, str]
    annotations: dict[str, str]


def _bad_request(message: str) -> ApiError:
    return ApiError(400, "BAD_REQUEST", message)


def conflict(message: str, reason: ConflictReason) -> ApiError:
    """409 错误：``details.reason`` 说明拒绝写入的原因。"""
    return ApiError(409, "CONFLICT", message, details={"reason": reason.value})


def check_update(
    document: Any,
    current: Resource,
    *,
    tenant_id: str,
    if_match: IfMatch | None,
) -> DesiredState:
    """按模块说明的规则检查更新文档，返回要写入的期望状态。

    Args:
        document: 更新文档（PUT 请求体或 PATCH 合并结果）。
        current: 当前资源（本次请求读到的版本）。
        tenant_id: 已验证凭证绑定的租户。
        if_match: 解析后的 ``If-Match``；没有该请求头时为 ``None``。

    Raises:
        ApiError: 文档不合法或试图修改不可变字段（400）；前置条件不满足（409）。
    """
    if not isinstance(document, dict):
        raise _bad_request("the resource document must be a JSON object")
    if unknown := unknown_fields(document):
        raise ApiError(
            400, "BAD_REQUEST", f"unknown top-level fields: {unknown}", details={"fields": unknown}
        )
    if "kind" in document and document["kind"] != current.kind:
        raise _bad_request(f"kind is immutable: expected {current.kind!r}")
    if "apiVersion" in document and document["apiVersion"] != current.apiVersion:
        raise _bad_request(f"apiVersion is immutable: expected {current.apiVersion!r}")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise _bad_request("metadata must be an object")
    meta = current.metadata
    if "name" in metadata and metadata["name"] != meta.name:
        raise _bad_request(f"metadata.name must match the name in the path ({meta.name!r})")
    if "namespace" in metadata and metadata["namespace"] != meta.namespace:
        raise _bad_request(
            f"metadata.namespace must match the namespace query parameter ({meta.namespace!r})"
        )
    for key in ("tenantId", "tenant_id"):
        if metadata.get(key) is not None and metadata[key] != tenant_id:
            raise _bad_request("the tenant of a resource is immutable")
    for key in ("uid", "resourceVersion"):
        if metadata.get(key) is not None and not isinstance(metadata[key], str):
            raise _bad_request(f"metadata.{key} must be a string")
    uid = metadata.get("uid")
    if uid is not None and uid != meta.uid:
        raise conflict(
            f"metadata.uid {uid!r} does not match the current resource {meta.uid!r}",
            ConflictReason.UID,
        )
    resource_version = metadata.get("resourceVersion")
    if resource_version is not None and resource_version != meta.resourceVersion:
        raise conflict(
            f"metadata.resourceVersion {resource_version!r} does not match the current "
            f"version {meta.resourceVersion!r}",
            ConflictReason.RESOURCE_VERSION,
        )
    if if_match is not None and not if_match.matches(etag_of(current)):
        raise conflict(
            f"If-Match does not match the current ETag {etag_of(current)}",
            ConflictReason.RESOURCE_VERSION,
        )

    labels = metadata.get("labels")
    annotations = metadata.get("annotations")
    candidate: dict[str, Any] = {
        "apiVersion": current.apiVersion,
        "kind": current.kind,
        "metadata": {
            **{k: v for k, v in metadata.items() if k not in SERVER_OWNED_METADATA},
            "name": meta.name,
            "namespace": meta.namespace,
            "labels": {} if labels is None else labels,
            "annotations": {} if annotations is None else annotations,
        },
    }
    if "spec" in document:
        candidate["spec"] = document["spec"]
    try:
        validated = InferenceService.model_validate(candidate)
    except ValidationError as exc:
        raise ApiError(
            400,
            "BAD_REQUEST",
            "inference service update failed validation",
            details={"errors": validation_details(exc)},
        ) from exc
    return DesiredState(
        spec=validated.spec.model_dump(mode="json"),
        labels=dict(validated.metadata.labels),
        annotations=dict(validated.metadata.annotations),
    )


__all__ = [
    "MERGE_PATCH_MEDIA_TYPE",
    "RESOURCE_FIELDS",
    "SERVER_OWNED_METADATA",
    "DesiredState",
    "IfMatch",
    "apply_merge_patch",
    "check_update",
    "conflict",
    "etag_of",
    "parse_if_match",
    "unknown_fields",
]
