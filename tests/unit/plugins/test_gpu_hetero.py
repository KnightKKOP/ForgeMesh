"""异构 GPU 能力差异报告单元测试。

覆盖：

* **report_diff** — 无差异 / missing / different / extra / 多字段 / 完整性。
* **classify_severity** — critical / warning / info 各路径。
* **format_report** — 空报告 / 含差异报告 / 汇总统计。

核心原则验证：**能力差异不隐藏**，所有差异必须被显式报告。
"""

from __future__ import annotations

from typing import Any

import pytest
from gpu_nvidia_plugin.hetero import (
    CRITICAL,
    DIFF_DIFFERENT,
    DIFF_EXTRA,
    DIFF_MISSING,
    INFO,
    WARNING,
    CapabilityDiff,
    HeteroReporter,
)

# --------------------------------------------------------------------------- #
# report_diff
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_report_diff_empty_when_capabilities_identical() -> None:
    """两方能力完全一致时应返回空差异列表。"""
    reporter = HeteroReporter()
    caps = {"mig_enabled": True, "memory_bytes": 80 * 1024**3, "name": "H100"}

    diffs = reporter.report_diff(caps, caps)

    assert diffs == []


@pytest.mark.unit
def test_report_diff_missing_when_other_vendor_lacks_field() -> None:
    """NVIDIA 有某字段、其他厂商缺失时，应报告 missing。"""
    reporter = HeteroReporter()
    nvidia = {"mig_enabled": True, "memory_bytes": 80}
    other = {"memory_bytes": 80}

    diffs = reporter.report_diff(nvidia, other)

    assert len(diffs) == 1
    assert diffs[0].field_name == "mig_enabled"
    assert diffs[0].diff_type == DIFF_MISSING
    assert diffs[0].nvidia_value is True
    assert diffs[0].other_vendor_value is None


@pytest.mark.unit
def test_report_diff_extra_when_nvidia_lacks_field() -> None:
    """其他厂商有某字段、NVIDIA 缺失时，应报告 extra。"""
    reporter = HeteroReporter()
    nvidia = {"memory_bytes": 80}
    other = {"memory_bytes": 80, "rocm_version": "6.0"}

    diffs = reporter.report_diff(nvidia, other)

    assert len(diffs) == 1
    assert diffs[0].field_name == "rocm_version"
    assert diffs[0].diff_type == DIFF_EXTRA
    assert diffs[0].nvidia_value is None
    assert diffs[0].other_vendor_value == "6.0"


@pytest.mark.unit
def test_report_diff_different_when_values_disagree() -> None:
    """两方都有某字段但值不同时，应报告 different。"""
    reporter = HeteroReporter()
    nvidia = {"memory_bytes": 80, "name": "H100"}
    other = {"memory_bytes": 64, "name": "H100"}

    diffs = reporter.report_diff(nvidia, other)

    assert len(diffs) == 1
    assert diffs[0].field_name == "memory_bytes"
    assert diffs[0].diff_type == DIFF_DIFFERENT
    assert diffs[0].nvidia_value == 80
    assert diffs[0].other_vendor_value == 64


@pytest.mark.unit
def test_report_diff_multiple_fields_all_reported() -> None:
    """多字段差异应全部报告，不遗漏。"""
    reporter = HeteroReporter()
    nvidia = {"mig_enabled": True, "memory_bytes": 80, "cuda_version": "12.2"}
    other = {"memory_bytes": 64, "rocm_version": "6.0", "cuda_version": "12.0"}

    diffs = reporter.report_diff(nvidia, other)

    # mig_enabled: missing, memory_bytes: different, rocm_version: extra, cuda_version: different
    by_field = {d.field_name: d for d in diffs}
    assert set(by_field) == {"mig_enabled", "memory_bytes", "rocm_version", "cuda_version"}
    assert by_field["mig_enabled"].diff_type == DIFF_MISSING
    assert by_field["memory_bytes"].diff_type == DIFF_DIFFERENT
    assert by_field["rocm_version"].diff_type == DIFF_EXTRA
    assert by_field["cuda_version"].diff_type == DIFF_DIFFERENT


@pytest.mark.unit
def test_report_diff_completeness_no_difference_hidden() -> None:
    """完整性：两方字段并集中每个有差异的字段都必须出现在结果中。

    这是核心原则的直接验证——能力差异不隐藏。
    """
    reporter = HeteroReporter()
    nvidia = {"a": 1, "b": 2, "c": 3, "d": 4}
    other = {"a": 1, "b": 99, "e": 5, "f": 6}

    diffs = reporter.report_diff(nvidia, other)
    reported_fields = {d.field_name for d in diffs}

    # 手动计算期望的差异字段
    all_fields = set(nvidia) | set(other)
    expected_diff_fields: set[str] = set()
    for field in all_fields:
        in_n = field in nvidia
        in_o = field in other
        if in_n and not in_o or not in_n and in_o or nvidia[field] != other[field]:
            expected_diff_fields.add(field)

    assert reported_fields == expected_diff_fields
    # a 相同，不应出现
    assert "a" not in reported_fields
    # b/c/d/e/f 都有差异，应全部出现
    assert {"b", "c", "d", "e", "f"} <= reported_fields


@pytest.mark.unit
def test_report_diff_empty_capabilities() -> None:
    """两方均为空 dict 时应返回空差异列表。"""
    reporter = HeteroReporter()
    assert reporter.report_diff({}, {}) == []


@pytest.mark.unit
def test_report_diff_all_missing() -> None:
    """其他厂商完全空时，NVIDIA 所有字段应报告 missing。"""
    reporter = HeteroReporter()
    nvidia = {"mig_enabled": True, "memory_bytes": 80}

    diffs = reporter.report_diff(nvidia, {})

    assert len(diffs) == 2
    assert all(d.diff_type == DIFF_MISSING for d in diffs)


@pytest.mark.unit
def test_report_diff_all_extra() -> None:
    """NVIDIA 完全空时，其他厂商所有字段应报告 extra。"""
    reporter = HeteroReporter()
    other = {"rocm_version": "6.0", "name": "MI300X"}

    diffs = reporter.report_diff({}, other)

    assert len(diffs) == 2
    assert all(d.diff_type == DIFF_EXTRA for d in diffs)


# --------------------------------------------------------------------------- #
# classify_severity
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_classify_severity_critical_for_missing_critical_field() -> None:
    """关键字段缺失应分类为 critical。"""
    reporter = HeteroReporter()
    diff = CapabilityDiff(
        field_name="mig_enabled",
        nvidia_value=True,
        other_vendor_value=None,
        diff_type=DIFF_MISSING,
    )
    assert reporter.classify_severity(diff) == CRITICAL


@pytest.mark.unit
def test_classify_severity_critical_for_different_critical_field() -> None:
    """关键字段值不同应分类为 critical。"""
    reporter = HeteroReporter()
    diff = CapabilityDiff(
        field_name="memory_bytes",
        nvidia_value=80,
        other_vendor_value=64,
        diff_type=DIFF_DIFFERENT,
    )
    assert reporter.classify_severity(diff) == CRITICAL


@pytest.mark.unit
def test_classify_severity_warning_for_missing_non_critical_field() -> None:
    """非关键字段缺失应分类为 warning。"""
    reporter = HeteroReporter()
    diff = CapabilityDiff(
        field_name="name",
        nvidia_value="H100",
        other_vendor_value=None,
        diff_type=DIFF_MISSING,
    )
    assert reporter.classify_severity(diff) == WARNING


@pytest.mark.unit
def test_classify_severity_warning_for_different_non_critical_field() -> None:
    """非关键字段值不同应分类为 warning。"""
    reporter = HeteroReporter()
    diff = CapabilityDiff(
        field_name="name",
        nvidia_value="H100",
        other_vendor_value="MI300X",
        diff_type=DIFF_DIFFERENT,
    )
    assert reporter.classify_severity(diff) == WARNING


@pytest.mark.unit
def test_classify_severity_info_for_extra() -> None:
    """其他厂商额外能力应分类为 info（不影响 NVIDIA 路径）。"""
    reporter = HeteroReporter()
    diff = CapabilityDiff(
        field_name="rocm_version",
        nvidia_value=None,
        other_vendor_value="6.0",
        diff_type=DIFF_EXTRA,
    )
    assert reporter.classify_severity(diff) == INFO


@pytest.mark.unit
def test_classify_severity_all_critical_fields() -> None:
    """所有关键字段缺失都应分类为 critical。"""
    reporter = HeteroReporter()
    critical_fields = ["mig_enabled", "memory_bytes", "compute_capability",
                       "cuda_version", "driver_version"]
    for field in critical_fields:
        diff = CapabilityDiff(
            field_name=field,
            nvidia_value="x",
            other_vendor_value=None,
            diff_type=DIFF_MISSING,
        )
        assert reporter.classify_severity(diff) == CRITICAL


# --------------------------------------------------------------------------- #
# format_report
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_format_report_empty_diffs() -> None:
    """无差异时报告应说明能力完全一致。"""
    reporter = HeteroReporter()
    text = reporter.format_report([])

    assert "无异构能力差异" in text
    assert "完全一致" in text


@pytest.mark.unit
def test_format_report_contains_all_diffs() -> None:
    """报告文本应包含每项差异的字段名与值。"""
    reporter = HeteroReporter()
    diffs = [
        CapabilityDiff(field_name="mig_enabled", nvidia_value=True,
                       other_vendor_value=None, diff_type=DIFF_MISSING),
        CapabilityDiff(field_name="memory_bytes", nvidia_value=80,
                       other_vendor_value=64, diff_type=DIFF_DIFFERENT),
        CapabilityDiff(field_name="rocm_version", nvidia_value=None,
                       other_vendor_value="6.0", diff_type=DIFF_EXTRA),
    ]

    text = reporter.format_report(diffs)

    assert "异构能力差异报告" in text
    assert "共 3 项差异" in text
    assert "mig_enabled" in text
    assert "memory_bytes" in text
    assert "rocm_version" in text
    # 严重度标签
    assert "[critical]" in text
    assert "[info]" in text


@pytest.mark.unit
def test_format_report_summary_counts() -> None:
    """报告汇总应正确统计 critical / warning / info 数量。"""
    reporter = HeteroReporter()
    diffs = [
        CapabilityDiff(field_name="mig_enabled", nvidia_value=True,
                       other_vendor_value=None, diff_type=DIFF_MISSING),       # critical
        CapabilityDiff(field_name="name", nvidia_value="H100",
                       other_vendor_value="MI300X", diff_type=DIFF_DIFFERENT),  # warning
        CapabilityDiff(field_name="rocm_version", nvidia_value=None,
                       other_vendor_value="6.0", diff_type=DIFF_EXTRA),         # info
    ]

    text = reporter.format_report(diffs)

    assert "critical=1" in text
    assert "warning=1" in text
    assert "info=1" in text


@pytest.mark.unit
def test_format_report_roundtrip_with_report_diff() -> None:
    """report_diff 产出的差异经 format_report 应可读且包含全部字段。"""
    reporter = HeteroReporter()
    nvidia: dict[str, Any] = {"mig_enabled": True, "memory_bytes": 80, "name": "H100"}
    other: dict[str, Any] = {"memory_bytes": 64, "name": "H100", "rocm_version": "6.0"}

    diffs = reporter.report_diff(nvidia, other)
    text = reporter.format_report(diffs)

    # 每个差异字段都应在文本中出现
    for diff in diffs:
        assert diff.field_name in text
