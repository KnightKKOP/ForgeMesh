"""``apply -f`` 的资源文档。

文件是 YAML（可以有多个以 ``---`` 分隔的文档）或 JSON（一个对象，或对象的数组），``-``
读取标准输入；顶层为数组（YAML 序列）时每一项是一个文档，``list -o json`` 的输出因此
可以直接再次提交。全部文档在发出任何请求之前读取并检查。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from .config import check_namespace, check_tenant
from .errors import UsageError

KIND = "InferenceService"
STDIN = "-"


@dataclass(frozen=True)
class Document:
    """一个待提交的推理服务文档。

    Attributes:
        source: 消息中的来源：文件名，文件中有多个文档时附带序号。
        body: 请求体；文档没有命名空间而给出了 ``--namespace`` 时已填入它。
        name: ``metadata.name``。
        namespace: 命名空间；``None`` 指不带命名空间的资源。
        tenant: 作为 ``X-Tenant-Id`` 发送的租户：文档的 ``metadata.tenantId``，其次
            ``--tenant``。API 以凭证确定租户，文档中的租户与 key 不符时请求被拒绝（``403``），
            而不是写进 key 的租户。
    """

    source: str
    body: dict[str, Any]
    name: str
    namespace: str | None
    tenant: str | None


def load_documents(
    paths: Sequence[str], *, namespace: str | None, tenant: str | None, stdin: TextIO
) -> list[Document]:
    """读取并检查 ``paths`` 中的全部文档。

    Args:
        paths: 文件路径，``-`` 为标准输入（最多一次）。
        namespace: ``--namespace``：文档没有命名空间时使用，有时必须与之相同。
        tenant: ``--tenant``：文档没有租户时使用，有时必须与之相同。
        stdin: 标准输入。

    Raises:
        UsageError: 文件不可读、不是合法的 YAML / JSON，或某个文档不是推理服务。
    """
    if sum(path == STDIN for path in paths) > 1:
        raise UsageError("standard input ('-') can be read only once")
    documents: list[Document] = []
    for path in paths:
        label = "<stdin>" if path == STDIN else path
        values = _parse(stdin.read() if path == STDIN else _read(path), label)
        if not values:
            raise UsageError(f"{label}: no documents found")
        for index, value in enumerate(values, start=1):
            source = label if len(values) == 1 else f"{label} (document {index})"
            documents.append(_document(value, source, namespace, tenant))
    return documents


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UsageError(f"{path} is not UTF-8 text") from exc
    except OSError as exc:
        raise UsageError(f"cannot read {path}: {exc.strerror or exc}") from exc


def _parse(text: str, label: str) -> list[Any]:
    try:
        return _load(text)
    except yaml.YAMLError as exc:
        raise UsageError(f"{label} is not valid YAML or JSON: {exc}") from exc
    except RecursionError as exc:
        raise UsageError(f"{label} is nested too deeply") from exc


def _load(text: str) -> list[Any]:
    if text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
        except ValueError:
            pass  # YAML 的流式写法同样以 { 或 [ 开头，交给 YAML 解析
        else:
            return value if isinstance(value, list) else [value]
    values = [value for value in yaml.safe_load_all(text) if value is not None]
    if len(values) == 1 and isinstance(values[0], list):
        return values[0]
    return values


def _document(value: Any, source: str, namespace: str | None, tenant: str | None) -> Document:
    if not isinstance(value, dict):
        raise UsageError(f"{source}: a resource document must be a mapping")
    try:
        _check_json(value, "")
    except RecursionError as exc:
        raise UsageError(f"{source}: the document is nested too deeply") from exc
    except ValueError as exc:
        raise UsageError(f"{source}: {exc}") from exc
    kind = value.get("kind", KIND)
    if kind != KIND:
        raise UsageError(f"{source}: kind {kind!r} is not supported; the CLI applies {KIND}")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        raise UsageError(f"{source}: metadata must be a mapping")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise UsageError(f"{source}: metadata.name must be a non-empty string")

    own_namespace = metadata.get("namespace")
    if own_namespace is None:
        own_namespace = namespace
        if namespace is not None:
            value = {**value, "metadata": {**metadata, "namespace": namespace}}
    elif not isinstance(own_namespace, str):
        raise UsageError(f"{source}: metadata.namespace must be a string")
    else:
        check_namespace(own_namespace, f"{source}: metadata.namespace")
        if namespace is not None and namespace != own_namespace:
            raise UsageError(
                f"{source}: metadata.namespace {own_namespace!r} does not match "
                f"--namespace {namespace!r}"
            )

    claims = [metadata[key] for key in ("tenantId", "tenant_id") if metadata.get(key) is not None]
    if any(not isinstance(claim, str) for claim in claims) or len(set(claims)) > 1:
        raise UsageError(f"{source}: metadata.tenantId must be a single string")
    own_tenant = claims[0] if claims else None
    if own_tenant is not None:
        check_tenant(own_tenant, f"{source}: metadata.tenantId")
        if tenant is not None and tenant != own_tenant:
            raise UsageError(
                f"{source}: metadata.tenantId {own_tenant!r} does not match --tenant {tenant!r}"
            )
    return Document(
        source=source,
        body=value,
        name=name,
        namespace=own_namespace,
        tenant=own_tenant if own_tenant is not None else tenant,
    )


def _check_json(value: Any, path: str) -> None:
    """检查 YAML 解析结果能否表示为 JSON（请求体是 JSON）。

    Raises:
        ValueError: 含有非字符串的键，或 JSON 无法表示的值（如未加引号的日期、``.nan``）。
    """
    where = path or "the document"
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{where} has a key that is not a string: {key!r}")
            _check_json(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_json(item, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where} is {value}, which JSON cannot represent")
    elif value is not None and not isinstance(value, str | int | float):
        raise ValueError(
            f"{where} is a {type(value).__name__} value, which JSON cannot represent; "
            "quote it to make it a string"
        )
