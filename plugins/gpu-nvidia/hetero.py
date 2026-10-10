"""异构 GPU 能力差异显式报告。

当集群中存在非 NVIDIA GPU（如 AMD ROCm / Intel oneAPI）时，其能力与
NVIDIA 不完全一致。本模块**显式报告所有能力差异，不隐藏任何差异**，
供调度器据此决定是否降级或拒绝跨厂商调度。

架构不变量：

* **差异不隐藏**（核心原则）：``report_diff`` 必须报告 NVIDIA 与其他厂商
  之间的每一个能力差异（缺失 / 不同 / 额外），调用方据此做显式决策，
  严禁静默忽略。
* **严重度可分类**：``classify_severity`` 将每项差异分为
  ``critical`` / ``warning`` / ``info``，关键字段差异直接影响调度正确性。
* **人类可读**：``format_report`` 生成可直接展示的差异报告文本。

兼容 Python 3.9+ / Pydantic v2。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

# --------------------------------------------------------------------------- #
# 差异类型
# --------------------------------------------------------------------------- #

#: NVIDIA 具备该能力，其他厂商缺失。
DIFF_MISSING = "missing"
#: 两边都具备该能力但值不同。
DIFF_DIFFERENT = "different"
#: 其他厂商具备该能力，NVIDIA 缺失。
DIFF_EXTRA = "extra"

# --------------------------------------------------------------------------- #
# 严重度
# --------------------------------------------------------------------------- #

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

#: 关键能力字段：其缺失或差异直接影响调度正确性，视为 ``critical``。
_CRITICAL_FIELDS: frozenset[str] = frozenset({
    "mig_enabled",
    "memory_bytes",
    "compute_capability",
    "cuda_version",
    "driver_version",
})


class CapabilityDiff(BaseModel):
    """单项能力差异。

    Attributes:
        field_name: 能力字段名。
        nvidia_value: NVIDIA 侧的值（NVIDIA 缺失该字段时为 ``None``）。
        other_vendor_value: 其他厂商侧的值（其他厂商缺失该字段时为 ``None``）。
        diff_type: 差异类型（``missing`` / ``different`` / ``extra``）。
    """

    model_config = ConfigDict(extra="forbid")

    field_name: str
    nvidia_value: Any = None
    other_vendor_value: Any = None
    diff_type: str


class HeteroReporter:
    """异构能力差异报告器。

    核心原则：**能力差异不隐藏**。``report_diff`` 遍历两方所有字段的并集，
    对每个存在差异的字段生成 :class:`CapabilityDiff`，确保不遗漏。
    """

    def report_diff(
        self,
        nvidia_capability: dict[str, Any],
        other_capability: dict[str, Any],
    ) -> list[CapabilityDiff]:
        """显式报告 NVIDIA 与其他厂商的能力差异。

        遍历两方所有字段的并集，对每个字段：

        * 仅 NVIDIA 有 → ``missing``（其他厂商缺失该能力）。
        * 仅其他厂商有 → ``extra``（NVIDIA 缺失该能力）。
        * 两方都有但值不同 → ``different``。
        * 两方都有且值相同 → 不报告（无差异）。

        Args:
            nvidia_capability: NVIDIA 设备能力字典。
            other_capability: 其他厂商设备能力字典。

        Returns:
            所有能力差异列表；空列表表示两方能力完全一致。
        """
        diffs: list[CapabilityDiff] = []
        all_fields = set(nvidia_capability) | set(other_capability)
        for field in sorted(all_fields):
            in_nvidia = field in nvidia_capability
            in_other = field in other_capability
            if in_nvidia and not in_other:
                diffs.append(CapabilityDiff(
                    field_name=field,
                    nvidia_value=nvidia_capability[field],
                    other_vendor_value=None,
                    diff_type=DIFF_MISSING,
                ))
            elif not in_nvidia and in_other:
                diffs.append(CapabilityDiff(
                    field_name=field,
                    nvidia_value=None,
                    other_vendor_value=other_capability[field],
                    diff_type=DIFF_EXTRA,
                ))
            elif nvidia_capability[field] != other_capability[field]:
                diffs.append(CapabilityDiff(
                    field_name=field,
                    nvidia_value=nvidia_capability[field],
                    other_vendor_value=other_capability[field],
                    diff_type=DIFF_DIFFERENT,
                ))
        return diffs

    def classify_severity(self, diff: CapabilityDiff) -> str:
        """差异严重度分类。

        分类规则：

        * ``extra``（其他厂商额外能力）→ ``info``：不影响 NVIDIA 调度路径。
        * 关键字段（``mig_enabled`` / ``memory_bytes`` / ``compute_capability``
          / ``cuda_version`` / ``driver_version``）的 ``missing`` 或
          ``different`` → ``critical``：直接影响调度正确性。
        * 非关键字段的 ``missing`` 或 ``different`` → ``warning``。

        Args:
            diff: 单项能力差异。

        Returns:
            严重度字符串（``critical`` / ``warning`` / ``info``）。
        """
        if diff.diff_type == DIFF_EXTRA:
            return INFO
        if diff.field_name in _CRITICAL_FIELDS:
            return CRITICAL
        return WARNING

    def format_report(self, diffs: list[CapabilityDiff]) -> str:
        """生成人类可读的差异报告。

        Args:
            diffs: 能力差异列表。

        Returns:
            多行文本报告，含逐项差异与汇总统计。
        """
        if not diffs:
            return "无异构能力差异：NVIDIA 与其他厂商能力完全一致。"

        lines: list[str] = ["异构能力差异报告："]
        lines.append(f"  共 {len(diffs)} 项差异：")
        for diff in diffs:
            severity = self.classify_severity(diff)
            lines.append(
                f"  [{severity}] {diff.field_name} "
                f"(类型={diff.diff_type}): "
                f"NVIDIA={diff.nvidia_value!r}, "
                f"其他厂商={diff.other_vendor_value!r}"
            )

        critical = sum(1 for d in diffs if self.classify_severity(d) == CRITICAL)
        warning = sum(1 for d in diffs if self.classify_severity(d) == WARNING)
        info = sum(1 for d in diffs if self.classify_severity(d) == INFO)
        lines.append(f"  汇总: critical={critical}, warning={warning}, info={info}")
        return "\n".join(lines)


__all__ = [
    "CapabilityDiff",
    "HeteroReporter",
    "DIFF_MISSING",
    "DIFF_DIFFERENT",
    "DIFF_EXTRA",
    "CRITICAL",
    "WARNING",
    "INFO",
]
