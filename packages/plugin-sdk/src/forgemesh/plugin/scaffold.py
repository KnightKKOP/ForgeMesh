"""插件项目脚手架生成器（对齐 development-plan 5.7 / WP-5.3）。

用字符串模板（不依赖 Jinja2）渲染插件项目骨架，支持 6 种插件类型：
``decision`` / ``workflow`` / ``runtime`` / ``gpu`` / ``metering`` / ``qos``。

生成的项目结构::

    <output_dir>/<name>/
        __init__.py
        provider.py
        manifest.yaml
        tests/
            __init__.py
            test_provider.py

设计要点：

* **零外部依赖**：:class:`ScaffoldTemplate` 用正则替换 ``{{ var }}`` 占位符，
  不引入 Jinja2 等模板引擎。
* **kind → category 映射**：脚手架 kind 与 :class:`PluginCategory` 的对应关系
  由 :data:`KIND_CATEGORY_MAP` 显式声明，便于审计。
* **合法标识符**：插件名只允许 ``[a-z][a-z0-9-]*``；目录名保留连字符，
  Python 模块名用下划线形式，测试骨架用 ``importlib`` 按文件路径加载
  ``provider.py``，兼容连字符目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# kind → PluginCategory 映射
# ---------------------------------------------------------------------------

#: 脚手架 kind 到 PluginManifest spec.category 的映射。
KIND_CATEGORY_MAP: dict[str, str] = {
    "decision": "policy",
    "workflow": "capability",
    "runtime": "runtime",
    "gpu": "hardware",
    "metering": "observability",
    "qos": "policy",
}

#: 支持的插件类型。
SUPPORTED_KINDS: tuple[str, ...] = tuple(KIND_CATEGORY_MAP.keys())

#: 合法插件名：小写字母开头，允许小写字母/数字/连字符。
_VALID_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class ScaffoldError(ValueError):
    """脚手架生成错误（非法 kind / 非法插件名 / 输出目录冲突等）。"""


def _to_pascal_case(name: str) -> str:
    """将插件名转为 PascalCase 类名。

    Examples:
        >>> _to_pascal_case("my-decision-plugin")
        'MyDecisionPlugin'
        >>> _to_pascal_case("runtime_vllm")
        'RuntimeVllm'
    """
    parts = re.split(r"[-_]", name)
    return "".join(part.capitalize() for part in parts if part)


def _to_module_name(name: str) -> str:
    """将插件名转为合法 Python 模块名（连字符 → 下划线）。"""
    return name.replace("-", "_")


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------

_INIT_TEMPLATE = '''"""{{ name }} 插件包。"""

from __future__ import annotations
'''

_MANIFEST_TEMPLATE = """\
apiVersion: forgemesh.io/v1alpha1
kind: PluginManifest
metadata:
  id: {{ name }}
  version: 0.1.0
spec:
  category: {{ category }}
  compatibility:
    coreApi: v1alpha1
  provides:
    - {{ name }}
  requires: []
  permissions: []
  isolation: restricted-rpc
  entrypoint: "provider:{{ class_name }}"
"""

# 各 kind 的 provider.py 模板。
_PROVIDER_TEMPLATES: dict[str, str] = {
    "decision": '''\
"""{{ name }} —— decision 插件 provider。

契约：实现 ``evaluate(context) -> dict`` 返回决策结果。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} decision provider。"""

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        """评估决策。

        Args:
            context: 决策上下文（请求特征、租户信息等）。

        Returns:
            决策结果字典，至少包含 ``decision`` 与 ``reason`` 键。
        """
        # TODO: 实现决策逻辑
        return {"decision": "allow", "reason": "default-allow"}
''',
    "workflow": '''\
"""{{ name }} —— workflow 插件 provider。

契约：实现 ``execute(workflow_def, inputs) -> dict`` 执行工作流。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} workflow runtime provider。"""

    def execute(
        self, workflow_def: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        """执行工作流定义。

        Args:
            workflow_def: 工作流定义（步骤、依赖等）。
            inputs: 工作流输入参数。

        Returns:
            执行结果字典，至少包含 ``status`` 与 ``outputs`` 键。
        """
        # TODO: 实现工作流执行逻辑
        return {"status": "succeeded", "outputs": {}}
''',
    "runtime": '''\
"""{{ name }} —— runtime 插件 provider。

契约：实现 ``start(config)`` / ``stop()`` / ``health()`` 管理运行时实例。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} runtime provider。"""

    def start(self, config: dict[str, Any]) -> dict[str, Any]:
        """启动运行时实例。

        Args:
            config: 运行时配置。

        Returns:
            启动结果，至少包含 ``instanceId`` 与 ``status`` 键。
        """
        # TODO: 实现启动逻辑
        return {"instanceId": "pending", "status": "Starting"}

    def stop(self, instance_id: str) -> dict[str, Any]:
        """停止运行时实例。"""
        # TODO: 实现停止逻辑
        return {"instanceId": instance_id, "status": "Stopped"}

    def health(self, instance_id: str) -> dict[str, Any]:
        """查询运行时实例健康状态。"""
        # TODO: 实现健康检查
        return {"instanceId": instance_id, "healthy": True}
''',
    "gpu": '''\
"""{{ name }} —— gpu 插件 provider。

契约：实现 ``discover()`` / ``allocate(request)`` 管理异构 GPU 资源。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} GPU provider。"""

    def discover(self) -> list[dict[str, Any]]:
        """发现可用 GPU 设备。

        Returns:
            设备信息列表，每项至少包含 ``deviceId`` / ``model`` / ``memory`` 键。
        """
        # TODO: 实现设备发现
        return []

    def allocate(self, request: dict[str, Any]) -> dict[str, Any]:
        """分配 GPU 资源。

        Args:
            request: 资源请求（模型、显存、数量等）。

        Returns:
            分配结果，至少包含 ``allocationId`` 与 ``devices`` 键。
        """
        # TODO: 实现分配逻辑
        return {"allocationId": "pending", "devices": []}
''',
    "metering": '''\
"""{{ name }} —— metering 插件 provider。

契约：实现 ``record(event)`` / ``aggregate(window)`` 计量与聚合。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} metering provider。"""

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        """记录一次计量事件。

        Args:
            event: 计量事件（资源用量、租户、时间戳等）。

        Returns:
            记录结果，至少包含 ``recorded`` 布尔键。
        """
        # TODO: 实现计量记录
        return {"recorded": True}

    def aggregate(self, window: dict[str, Any]) -> dict[str, Any]:
        """聚合指定时间窗口的计量数据。

        Args:
            window: 时间窗口（起止时间、租户、维度等）。

        Returns:
            聚合结果，至少包含 ``usage`` 键。
        """
        # TODO: 实现聚合逻辑
        return {"usage": {}}
''',
    "qos": '''\
"""{{ name }} —— qos 插件 provider。

契约：实现 ``apply(policy, target)`` / ``evaluate(metrics)`` 管理服务质量策略。
"""

from __future__ import annotations

from typing import Any


class {{ class_name }}:
    """{{ name }} QoS provider。"""

    def apply(self, policy: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
        """对目标应用 QoS 策略。

        Args:
            policy: QoS 策略定义（优先级、配额、限流等）。
            target: 目标资源标识。

        Returns:
            应用结果，至少包含 ``applied`` 布尔键。
        """
        # TODO: 实现策略应用
        return {"applied": True}

    def evaluate(self, metrics: dict[str, Any]) -> dict[str, Any]:
        """评估当前指标是否满足 QoS 目标。

        Args:
            metrics: 运行指标（延迟、吞吐、错误率等）。

        Returns:
            评估结果，至少包含 ``satisfied`` 布尔键。
        """
        # TODO: 实现评估逻辑
        return {"satisfied": True}
''',
}

# 各 kind 的 test_provider.py 模板（用 importlib 按路径加载 provider，兼容连字符目录）。
_TEST_TEMPLATES: dict[str, str] = {
    "decision": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_evaluate_returns_decision_dict() -> None:
    """evaluate 应返回包含 decision 键的字典。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.evaluate({})  # type: ignore[attr-defined]
    assert isinstance(result, dict)
    assert "decision" in result
''',
    "workflow": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_execute_returns_status_dict() -> None:
    """execute 应返回包含 status 键的字典。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.execute({}, {})  # type: ignore[attr-defined]
    assert isinstance(result, dict)
    assert "status" in result
''',
    "runtime": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_start_returns_instance_dict() -> None:
    """start 应返回包含 instanceId 键的字典。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.start({})  # type: ignore[attr-defined]
    assert isinstance(result, dict)
    assert "instanceId" in result
''',
    "gpu": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_discover_returns_list() -> None:
    """discover 应返回设备列表。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.discover()  # type: ignore[attr-defined]
    assert isinstance(result, list)
''',
    "metering": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_record_returns_recorded_flag() -> None:
    """record 应返回包含 recorded 键的字典。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.record({})  # type: ignore[attr-defined]
    assert isinstance(result, dict)
    assert "recorded" in result
''',
    "qos": '''\
"""{{ name }} provider 契约测试骨架。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"


def _load_provider() -> object:
    """按文件路径加载 provider.py（兼容连字符目录名）。"""
    spec = importlib.util.spec_from_file_location("{{ module_name }}_provider", _PROVIDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_apply_returns_applied_flag() -> None:
    """apply 应返回包含 applied 键的字典。"""
    module = _load_provider()
    provider = module.{{ class_name }}()  # type: ignore[attr-defined]
    result = provider.apply({}, {})  # type: ignore[attr-defined]
    assert isinstance(result, dict)
    assert "applied" in result
''',
}

_TEST_INIT_TEMPLATE = '''"""{{ name }} 插件测试包。"""

from __future__ import annotations
'''


# ---------------------------------------------------------------------------
# 模板渲染器
# ---------------------------------------------------------------------------


class ScaffoldTemplate:
    """字符串模板渲染器（``{{ var }}`` 占位符，不依赖 Jinja2）。

    用正则匹配 ``{{ key }}`` 并从上下文字典替换；未提供对应键时抛出
    :class:`ScaffoldError`，避免静默遗留占位符。
    """

    _VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

    def render(self, template: str, context: dict[str, Any]) -> str:
        """渲染模板，将 ``{{ key }}`` 替换为 ``str(context[key])``。

        Args:
            template: 含 ``{{ key }}`` 占位符的模板字符串。
            context: 占位符取值字典。

        Returns:
            渲染后的字符串。

        Raises:
            ScaffoldError: 模板中存在 ``context`` 未提供的占位符。
        """
        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise ScaffoldError(f"template variable not provided: {key!r}")
            return str(context[key])

        return self._VAR_RE.sub(_replace, template)


# ---------------------------------------------------------------------------
# 脚手架生成器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaffoldResult:
    """脚手架生成结果。

    Attributes:
        root: 生成的插件项目根目录。
        files: 生成的文件相对路径列表（相对 ``root``）。
    """

    root: Path
    files: list[str]


class ScaffoldGenerator:
    """插件项目脚手架生成器。

    根据 ``name`` 与 ``kind`` 生成完整的插件项目结构，包含
    ``__init__.py`` / ``provider.py`` / ``manifest.yaml`` / ``tests/`` 骨架。
    """

    def __init__(self, template: ScaffoldTemplate | None = None) -> None:
        self.template = template or ScaffoldTemplate()

    def generate(self, name: str, kind: str, output_dir: str | Path) -> Path:
        """生成完整插件项目结构。

        Args:
            name: 插件名（``[a-z][a-z0-9-]*``，如 ``my-decision-plugin``）。
            kind: 插件类型，取值见 :data:`SUPPORTED_KINDS`。
            output_dir: 输出根目录；插件项目将生成在 ``<output_dir>/<name>``。

        Returns:
            生成的插件项目根目录路径。

        Raises:
            ScaffoldError: ``kind`` 不支持、``name`` 不合法、或目标目录已存在。
        """
        self._validate_kind(kind)
        self._validate_name(name)

        context = {
            "name": name,
            "kind": kind,
            "category": KIND_CATEGORY_MAP[kind],
            "class_name": _to_pascal_case(name),
            "module_name": _to_module_name(name),
        }

        root = Path(output_dir) / name
        if root.exists():
            raise ScaffoldError(f"target directory already exists: {root}")

        files = self._render_project(root, kind, context)
        # 触发生成后校验：确保所有占位符已被替换。
        self._assert_no_placeholders(files)
        return root

    # -- 校验 -------------------------------------------------------------

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in KIND_CATEGORY_MAP:
            allowed = ", ".join(SUPPORTED_KINDS)
            raise ScaffoldError(f"unsupported kind: {kind!r} (allowed: {allowed})")

    @staticmethod
    def _validate_name(name: str) -> None:
        if not _VALID_NAME_RE.match(name):
            raise ScaffoldError(
                f"invalid plugin name: {name!r} (must match [a-z][a-z0-9-]*)"
            )

    @staticmethod
    def _assert_no_placeholders(files: list[tuple[Path, str]]) -> None:
        """确保生成的文件内容中无残留 ``{{ }}`` 占位符。"""
        for _path, content in files:
            if ScaffoldTemplate._VAR_RE.search(content):  # noqa: SLF001
                raise ScaffoldError(f"unresolved template placeholder in {_path}")

    # -- 渲染 -------------------------------------------------------------

    def _render_project(
        self, root: Path, kind: str, context: dict[str, Any]
    ) -> list[tuple[Path, str]]:
        """渲染并写出全部项目文件，返回 (路径, 内容) 列表供后校验。"""
        rendered: list[tuple[Path, str]] = []

        def _write(rel: str, template: str) -> None:
            content = self.template.render(template, context)
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            rendered.append((path, content))

        _write("__init__.py", _INIT_TEMPLATE)
        _write("provider.py", _PROVIDER_TEMPLATES[kind])
        _write("manifest.yaml", _MANIFEST_TEMPLATE)
        _write("tests/__init__.py", _TEST_INIT_TEMPLATE)
        _write("tests/test_provider.py", _TEST_TEMPLATES[kind])

        return rendered


__all__ = [
    "KIND_CATEGORY_MAP",
    "SUPPORTED_KINDS",
    "ScaffoldError",
    "ScaffoldTemplate",
    "ScaffoldGenerator",
    "ScaffoldResult",
]
