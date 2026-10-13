"""NVIDIA GPU 预检：驱动 / CUDA / 容器运行时校验。

在调度 GPU 工作负载前，验证宿主机满足最低驱动、CUDA 与容器运行时要求。
不满足时返回可诊断信息（``passed=False`` + ``message`` + ``details``），
供上层决定是否降级或拒绝调度。

架构不变量：

* **预检 ≠ 授权**：预检仅产出事实，不分配资源。
* **可诊断**：失败时携带 ``details``，便于运维定位。
* **测试友好**：各检查函数可注入 mock。

基于 Pydantic v2。
"""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: 信息提供者类型：返回包含 ``version`` 等键的 dict。
InfoProvider = Callable[[], Awaitable[dict[str, Any]]]


class PreflightCheck(BaseModel):
    """单项预检结果。

    Attributes:
        name: 检查项名称（如 ``driver`` / ``cuda`` / ``runtime``）。
        passed: 是否通过。
        message: 人类可读的结论说明。
        details: 诊断详情（实际值、最低要求、原始信息等）。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class PreflightResult(BaseModel):
    """预检汇总结果。

    Attributes:
        checks: 各项检查结果列表。
        all_passed: 是否全部通过。
        summary: 人类可读汇总。
    """

    model_config = ConfigDict(extra="forbid")

    checks: list[PreflightCheck] = Field(default_factory=list)
    all_passed: bool = True
    summary: str = ""


def _version_tuple(version: str) -> tuple[int, ...]:
    """将版本字符串解析为可比较的整数元组。

    仅取每段前导数字，忽略后缀（如 ``12.2a`` → ``(12, 2)``）。
    """
    parts: list[int] = []
    for segment in version.split("."):
        digits = ""
        for ch in segment:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _ge_version(actual: str, minimum: str) -> bool:
    """判断 ``actual >= minimum``（版本语义比较）。"""
    a = _version_tuple(actual)
    b = _version_tuple(minimum)
    n = max(len(a), len(b))
    a_padded = a + (0,) * (n - len(a))
    b_padded = b + (0,) * (n - len(b))
    return a_padded >= b_padded


async def _default_driver_info() -> dict[str, Any]:
    """默认驱动信息提供者：尝试 pynvml，不可用返回空版本。"""
    try:
        import pynvml as nvml  # noqa: PLC0415

        nvml.nvmlInit()
        version = str(nvml.nvmlSystemGetDriverVersion())
        nvml.nvmlShutdown()
    except Exception:
        return {"version": ""}
    else:
        return {"version": version}


async def _default_cuda_info() -> dict[str, Any]:
    """默认 CUDA 信息提供者：尝试 pynvml，不可用返回空版本。"""
    try:
        import pynvml as nvml  # noqa: PLC0415

        nvml.nvmlInit()
        cuda_version = nvml.nvmlSystemGetCudaDriverVersion()
        nvml.nvmlShutdown()
    except Exception:
        return {"version": ""}
    else:
        # pynvml 返回形如 12020 表示 12.2
        major = int(cuda_version) // 1000
        minor = (int(cuda_version) % 1000) // 10
        return {"version": f"{major}.{minor}"}


async def _default_runtime_info() -> dict[str, Any]:
    """默认运行时检查：检测 nvidia-container-runtime / toolkit 是否安装。"""
    runtime_path = shutil.which("nvidia-container-runtime")
    if runtime_path:
        return {"available": True, "name": "nvidia-container-runtime", "path": runtime_path}
    toolkit_path = shutil.which("nvidia-ctk")
    if toolkit_path:
        return {"available": True, "name": "nvidia-container-toolkit", "path": toolkit_path}
    return {"available": False, "name": "nvidia-container-runtime"}


class PreflightValidator:
    """预检校验器：驱动 / CUDA / 运行时。

    Args:
        driver_info_provider: 驱动信息提供者（可注入 mock）。
        cuda_info_provider: CUDA 信息提供者（可注入 mock）。
        runtime_info_provider: 运行时信息提供者（可注入 mock）。
    """

    def __init__(
        self,
        *,
        driver_info_provider: InfoProvider | None = None,
        cuda_info_provider: InfoProvider | None = None,
        runtime_info_provider: InfoProvider | None = None,
    ) -> None:
        self._get_driver_info: InfoProvider = driver_info_provider or _default_driver_info
        self._get_cuda_info: InfoProvider = cuda_info_provider or _default_cuda_info
        self._get_runtime_info: InfoProvider = runtime_info_provider or _default_runtime_info

    async def validate_driver(self, min_version: str = "535.0") -> PreflightCheck:
        """校验驱动版本是否满足最低要求。"""
        try:
            info = await self._get_driver_info()
        except Exception as exc:
            return PreflightCheck(
                name="driver",
                passed=False,
                message=f"驱动信息获取失败: {exc}",
                details={"error": str(exc), "min_version": min_version},
            )

        actual = str(info.get("version", ""))
        passed = bool(actual) and _ge_version(actual, min_version)
        if passed:
            message = f"驱动版本 {actual} 满足最低要求 {min_version}"
        else:
            message = f"驱动版本 '{actual}' 不满足最低要求 {min_version}"
        return PreflightCheck(
            name="driver",
            passed=passed,
            message=message,
            details={"actual": actual, "min_version": min_version, **info},
        )

    async def validate_cuda(self, min_version: str = "12.0") -> PreflightCheck:
        """校验 CUDA 版本是否满足最低要求。"""
        try:
            info = await self._get_cuda_info()
        except Exception as exc:
            return PreflightCheck(
                name="cuda",
                passed=False,
                message=f"CUDA 信息获取失败: {exc}",
                details={"error": str(exc), "min_version": min_version},
            )

        actual = str(info.get("version", ""))
        passed = bool(actual) and _ge_version(actual, min_version)
        if passed:
            message = f"CUDA 版本 {actual} 满足最低要求 {min_version}"
        else:
            message = f"CUDA 版本 '{actual}' 不满足最低要求 {min_version}"
        return PreflightCheck(
            name="cuda",
            passed=passed,
            message=message,
            details={"actual": actual, "min_version": min_version, **info},
        )

    async def validate_runtime(self) -> PreflightCheck:
        """校验容器运行时（nvidia-container-runtime / toolkit）是否可用。"""
        try:
            info = await self._get_runtime_info()
        except Exception as exc:
            return PreflightCheck(
                name="runtime",
                passed=False,
                message=f"运行时检查失败: {exc}",
                details={"error": str(exc)},
            )

        available = bool(info.get("available", False))
        runtime_name = str(info.get("name", "unknown"))
        if available:
            message = f"容器运行时 {runtime_name} 可用"
        else:
            message = f"容器运行时 {runtime_name} 不可用"
        return PreflightCheck(
            name="runtime",
            passed=available,
            message=message,
            details=info,
        )

    async def validate_all(self) -> PreflightResult:
        """执行全部预检（驱动 / CUDA / 运行时），返回汇总结果。"""
        checks = [
            await self.validate_driver(),
            await self.validate_cuda(),
            await self.validate_runtime(),
        ]
        all_passed = all(check.passed for check in checks)
        passed_count = sum(1 for check in checks if check.passed)
        summary = f"{passed_count}/{len(checks)} 项预检通过"
        return PreflightResult(checks=checks, all_passed=all_passed, summary=summary)


__all__ = [
    "PreflightCheck",
    "PreflightResult",
    "PreflightValidator",
    "InfoProvider",
]
