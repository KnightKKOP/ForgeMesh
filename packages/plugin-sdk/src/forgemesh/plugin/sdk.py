"""插件 SDK 核心：脚手架生成、清单校验、打包、发布（WP-5.3）。

:class:`PluginSdk` 是开发者面向的统一入口，串联脚手架生成（
:class:`forgemesh.plugin.scaffold.ScaffoldGenerator`）、清单校验（
:class:`forgemesh.plugin.manifest.PluginManifest`）、打包（tar.gz + SHA-256
摘要）与发布（注册中心上传）。

设计要点：

* **数据类分离**：:class:`ValidationResult` / :class:`PluginArtifact` /
  :class:`PublishResult` 为纯数据载体；制品签名由 :mod:`forgemesh.plugin.signing`
  负责，签名对象是打包产物的内容与清单。
* **校验分层**：``validate_manifest`` 先做 Schema 校验（pydantic），再做语义
  检查（未声明权限/隔离等告警），``valid`` 仅在无 error 时为真。
* **发布可模拟**：``publish`` 支持 ``memory://`` 协议用于本地/测试环境；
  ``http(s)://`` 走真实上传，失败时返回 ``success=False`` 而非抛异常，
  便于 CI 重试。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml
from forgemesh.plugin.manifest import PluginManifest
from forgemesh.plugin.scaffold import ScaffoldGenerator
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """清单校验结果。

    Attributes:
        valid: 无 error 时为真（warnings 不影响 validity）。
        errors: 阻断性错误列表。
        warnings: 非阻断性告警列表。
    """

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PluginArtifact:
    """打包制品（携带产物路径）。

    Attributes:
        name: 制品名称（来自 manifest metadata.id）。
        version: 制品语义版本（来自 manifest metadata.version）。
        digest: 制品内容 SHA-256 摘要（``sha256:<hex>``）。
        path: 打包产物（tar.gz）路径。
    """

    name: str
    version: str
    digest: str
    path: Path


@dataclass
class PublishResult:
    """发布结果。

    Attributes:
        success: 是否发布成功。
        url: 制品在注册中心的 URL（失败时为空串）。
        digest: 制品摘要（与 :class:`PluginArtifact.digest` 一致）。
    """

    success: bool
    url: str
    digest: str


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SdkError(Exception):
    """SDK 操作错误（打包/发布前置条件不满足等）。"""


# ---------------------------------------------------------------------------
# PluginSdk
# ---------------------------------------------------------------------------

#: 发布支持的协议前缀。
_PUBLISH_PROTOCOLS: tuple[str, ...] = ("memory://", "http://", "https://")

#: HTTP 发布超时（秒）。
_HTTP_TIMEOUT: float = 5.0


class PluginSdk:
    """插件 SDK 统一入口。

    串联脚手架生成、清单校验、打包与发布四个开发者工作流步骤。
    """

    def __init__(self, scaffold: ScaffoldGenerator | None = None) -> None:
        self.scaffold = scaffold or ScaffoldGenerator()

    # -- 脚手架 -----------------------------------------------------------

    def generate_scaffold(self, name: str, kind: str, output_dir: str | Path) -> Path:
        """生成插件项目骨架。

        Args:
            name: 插件名（``[a-z][a-z0-9-]*``）。
            kind: 插件类型（decision/workflow/runtime/gpu/metering/qos）。
            output_dir: 输出根目录。

        Returns:
            生成的插件项目根目录路径。
        """
        return self.scaffold.generate(name, kind, output_dir)

    # -- 清单校验 ---------------------------------------------------------

    def validate_manifest(self, manifest_path: str | Path) -> ValidationResult:
        """校验插件清单。

        校验分两层：

        1. **Schema 校验**：用 :class:`PluginManifest` 解析 YAML，捕获
           :class:`ValidationError` 收集为 errors。
        2. **语义告警**：解析成功后检查未声明权限/隔离/入口点等，记为 warnings。

        Args:
            manifest_path: ``manifest.yaml`` 路径。

        Returns:
            :class:`ValidationResult`；``valid`` 为真当且仅当无 error。
        """
        path = Path(manifest_path)
        errors: list[str] = []
        warnings: list[str] = []

        try:
            raw = self._load_yaml(path)
        except FileNotFoundError:
            return ValidationResult(valid=False, errors=[f"manifest not found: {path}"])
        except yaml.YAMLError as exc:
            return ValidationResult(valid=False, errors=[f"YAML parse error: {exc}"])

        try:
            manifest = PluginManifest.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"])
                errors.append(f"{loc}: {err['msg']}")
            return ValidationResult(valid=False, errors=errors)

        # 语义告警
        self._semantic_warnings(manifest, warnings)
        return ValidationResult(valid=True, errors=[], warnings=warnings)

    @staticmethod
    def _semantic_warnings(manifest: PluginManifest, warnings: list[str]) -> None:
        spec = manifest.spec
        if not spec.permissions:
            warnings.append("spec.permissions is empty; plugin will run with no capabilities")
        if spec.isolation is None:
            warnings.append("spec.isolation is unset; the plugin runs in-process")
        if spec.entrypoint is None:
            warnings.append("spec.entrypoint is unset; the plugin cannot be loaded")
        if not spec.compatibility:
            warnings.append("spec.compatibility is empty; core API version pinning recommended")

    # -- 打包 -------------------------------------------------------------

    def pack(self, plugin_dir: str | Path) -> PluginArtifact:
        """打包插件为 tar.gz 制品。

        将 ``plugin_dir`` 下全部文件（排除 ``__pycache__`` / ``.pytest_cache`` /
        ``tests/`` 产物）打入 tar.gz，计算 SHA-256 摘要，并从 ``manifest.yaml``
        读取名称与版本。

        Args:
            plugin_dir: 插件项目根目录（含 ``manifest.yaml``）。

        Returns:
            :class:`PluginArtifact`。

        Raises:
            SdkError: ``manifest.yaml`` 缺失或无法解析。
        """
        root = Path(plugin_dir)
        manifest_path = root / "manifest.yaml"
        if not manifest_path.is_file():
            raise SdkError(f"manifest.yaml not found in plugin dir: {root}")

        raw = self._load_yaml(manifest_path)
        try:
            manifest = PluginManifest.model_validate(raw)
        except ValidationError as exc:
            raise SdkError(f"invalid manifest: {exc}") from exc

        content = self._build_tarball(root)
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"

        artifact_path = root / f"{manifest.metadata.id}-{manifest.metadata.version}.tar.gz"
        artifact_path.write_bytes(content)

        return PluginArtifact(
            name=manifest.metadata.id,
            version=manifest.metadata.version,
            digest=digest,
            path=artifact_path,
        )

    @staticmethod
    def _build_tarball(root: Path) -> bytes:
        """将插件目录打包为 tar.gz 字节流，排除缓存与测试产物。"""
        exclude_dirs = {"__pycache__", ".pytest_cache", ".git", "node_modules"}
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for file_path in sorted(root.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(root)
                if any(part in exclude_dirs for part in rel.parts):
                    continue
                tar.add(file_path, arcname=str(rel))
        return buffer.getvalue()

    # -- 发布 -------------------------------------------------------------

    def publish(self, artifact: PluginArtifact, registry_url: str) -> PublishResult:
        """发布制品到注册中心。

        支持的协议：

        * ``memory://`` —— 本地/测试环境，直接返回成功。
        * ``http(s)://`` —— 真实上传，失败返回 ``success=False``。

        Args:
            artifact: :meth:`pack` 产出的制品。
            registry_url: 注册中心基址。

        Returns:
            :class:`PublishResult`。
        """
        if not any(registry_url.startswith(p) for p in _PUBLISH_PROTOCOLS):
            return PublishResult(success=False, url="", digest=artifact.digest)

        target_url = f"{registry_url.rstrip('/')}/{artifact.name}/{artifact.version}"

        if registry_url.startswith("memory://"):
            return PublishResult(success=True, url=target_url, digest=artifact.digest)

        # http(s):// —— 真实上传
        return self._http_publish(artifact, target_url)

    @staticmethod
    def _http_publish(artifact: PluginArtifact, target_url: str) -> PublishResult:
        """通过 HTTP POST 上传制品摘要元数据。"""
        payload = json.dumps(
            {
                "name": artifact.name,
                "version": artifact.version,
                "digest": artifact.digest,
                "uploadedAt": time.time(),
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            target_url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib_request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
                if 200 <= resp.status < 300:
                    return PublishResult(success=True, url=target_url, digest=artifact.digest)
                return PublishResult(success=False, url="", digest=artifact.digest)
        except (urllib_error.URLError, OSError, TimeoutError):
            return PublishResult(success=False, url="", digest=artifact.digest)

    # -- IO 辅助 ----------------------------------------------------------

    @staticmethod
    def _load_yaml(path: Path) -> dict[str, Any]:
        """读取 YAML 文件为字典。"""
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)


__all__ = [
    "ValidationResult",
    "PluginArtifact",
    "PublishResult",
    "SdkError",
    "PluginSdk",
]
