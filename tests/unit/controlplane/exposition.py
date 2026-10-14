"""Prometheus 文本格式（0.0.4）的严格解析，供测试校验 ``/metrics`` 的输出。

:func:`parse` 不合法时抛出 ``AssertionError``，检查：

* 文本以换行结尾、没有空行；``# HELP`` / ``# TYPE`` 每个指标至多一次，且在它的样本之前；
* 样本名属于已声明类型的指标（直方图为 ``_bucket`` / ``_sum`` / ``_count``），同一指标的
  样本连续出现；指标名、标签名合法，标签名不重复，标签值的转义合法，取值是浮点数；
* 同一序列（样本名 + 标签集合）只出现一次；
* 直方图的每个序列：``le`` 严格递增并以 ``+Inf`` 结尾，桶计数累积不减，``+Inf`` 桶等于
  ``_count``，且有 ``_sum``。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_HELP = re.compile(rf"^# HELP ({_NAME}) (.*)$")
_TYPE = re.compile(rf"^# TYPE ({_NAME}) (counter|gauge|histogram|summary|untyped)$")
_SAMPLE = re.compile(rf"^({_NAME})(?:\{{(.*)\}})? (\S+)$")
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"(,|$)')
_UNESCAPE = re.compile(r"\\([\\\"n])")


@dataclass
class Sample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass
class Family:
    name: str
    type: str = "untyped"
    help: str | None = None
    samples: list[Sample] = field(default_factory=list)

    def value(self, sample_name: str | None = None, **labels: str) -> float:
        """唯一匹配 ``labels`` 的样本取值（``sample_name`` 缺省为指标名）。"""
        wanted = sample_name or self.name
        matches = [s for s in self.samples if s.name == wanted and s.labels == labels]
        assert len(matches) == 1, f"{wanted}{labels}: {len(matches)} samples in {self.samples}"
        return matches[0].value


def _parse_labels(text: str, line: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    position = 0
    while position < len(text):
        match = _LABEL.match(text, position)
        assert match, f"malformed labels in {line!r}"
        name, raw, _ = match.groups()
        assert name not in labels, f"duplicate label {name!r} in {line!r}"
        labels[name] = _UNESCAPE.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), raw)
        position = match.end()
    return labels


def _family_of(sample: str, families: dict[str, Family]) -> Family:
    if sample in families and families[sample].type != "histogram":
        return families[sample]
    for suffix in ("_bucket", "_sum", "_count"):
        base = sample.removesuffix(suffix)
        if base != sample and families.get(base) is not None:
            assert families[base].type == "histogram", f"{sample} for non-histogram {base}"
            return families[base]
    raise AssertionError(f"sample {sample} has no # TYPE line")


def _check_histogram(family: Family) -> None:
    series: dict[tuple[tuple[str, str], ...], dict[str, object]] = {}
    for sample in family.samples:
        labels = dict(sample.labels)
        le = labels.pop("le", None)
        entry = series.setdefault(tuple(sorted(labels.items())), {"buckets": []})
        if sample.name.endswith("_bucket"):
            assert le is not None, f"bucket without le: {sample}"
            entry["buckets"].append((float(le), sample.value))
        else:
            assert le is None, f"le on {sample}"
            entry[sample.name.rsplit("_", 1)[1]] = sample.value
    for key, entry in series.items():
        buckets = entry["buckets"]
        assert buckets, f"{family.name}{key} has no buckets"
        bounds = [bound for bound, _ in buckets]
        counts = [count for _, count in buckets]
        assert bounds == sorted(set(bounds)), f"{family.name}{key} le not increasing: {bounds}"
        assert math.isinf(bounds[-1]), f"{family.name}{key} lacks the +Inf bucket"
        assert counts == sorted(counts), f"{family.name}{key} buckets not cumulative: {counts}"
        assert entry.get("count") == counts[-1], f"{family.name}{key} +Inf != _count"
        assert "sum" in entry, f"{family.name}{key} lacks _sum"


def parse(text: str) -> dict[str, Family]:
    """解析并校验文本格式，返回 指标名 → :class:`Family`。"""
    assert text.endswith("\n"), "exposition must end with a newline"
    families: dict[str, Family] = {}
    finished: set[str] = set()
    current: str | None = None
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for line in text[:-1].split("\n"):
        assert line, "empty line in exposition"
        if line.startswith("#"):
            if match := _HELP.match(line):
                name = match.group(1)
                family = families.setdefault(name, Family(name))
                assert family.help is None, f"second HELP for {name}"
                assert not family.samples, f"HELP after samples of {name}"
                family.help = match.group(2)
            elif match := _TYPE.match(line):
                name, kind = match.groups()
                family = families.setdefault(name, Family(name))
                assert family.type == "untyped" and not family.samples, f"late TYPE for {name}"
                family.type = kind
            else:
                assert not line.startswith(("# HELP", "# TYPE")), f"malformed {line!r}"
            continue
        match = _SAMPLE.match(line)
        assert match, f"malformed sample line {line!r}"
        name, label_text, raw_value = match.groups()
        family = _family_of(name, families)
        if family.name != current:
            assert family.name not in finished, f"samples of {family.name} are not contiguous"
            if current is not None:
                finished.add(current)
            current = family.name
        labels = _parse_labels(label_text or "", line)
        key = (name, tuple(sorted(labels.items())))
        assert key not in seen, f"duplicate series {line!r}"
        seen.add(key)
        value = float(raw_value)
        family.samples.append(Sample(name, labels, value))
    for family in families.values():
        if family.type == "histogram":
            _check_histogram(family)
    return families
