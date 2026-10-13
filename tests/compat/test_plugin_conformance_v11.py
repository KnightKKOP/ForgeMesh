"""WP-E1 插件可发布性 conformance 测试（v1.1 阶段 E，任务 64）。

对 10 个插件（decision-rules / decision-jev / decision-typesafe-jev /
workflow-reference / workflow-temporal / gpu-nvidia / runtime-vllm /
runtime-tgi / qos-basic / metering-basic）执行一致的发布验证链路：

* 第 1 节：manifest schema 校验 + isolation 声明与实际加载方式一致性；
* 第 2 节：Decision Provider 契约 conformance（正常/无效输入/超时降级）；
* 第 3 节：Workflow Provider 契约 conformance（start/查询/取消/幂等/NotFound）；
* 第 4 节：Runtime Provider 契约 conformance（deploy→verify→release 闭环）；
* 第 5 节：升级/卸载安全（运行中引用下的卸载语义、版本并存）。

标记说明：任务书称 ``@pytest.mark.compat`` 已注册，但 ``pyproject.toml`` 的
markers 列表仅含 unit/contract/integration/e2e/performance 且启用
``--strict-markers``，未注册的 marker 会使收集直接失败；同目录既有
``test_plugin_compatibility.py`` 以 ``contract`` 作为兼容性测试统一标记，
本文件沿用该先例（不修改共享配置）。

已记录的实现缺陷（测试按"当前行为"断言并在此备案，期望行为见各 docstring）：

* **DEFECT-E1-01（第 5 节）**：``PluginManager`` 允许 ``Enabled → Uninstalled``
  直接转移，且无运行中引用计数 / Draining 状态。即：一个能力正被 Router
  持有（运行中被引用）的插件可被直接卸载，缺少"引用归零后方可卸载"的
  治理闸门。期望：卸载请求应被拒绝，或先进入 Draining 等待在途引用释放。

兼容 Python 3.12、pytest。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from forgemesh.core.errors import (
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
)
from forgemesh.plugin.manifest import (
    PluginLifecycleState,
    PluginManifest,
)
from forgemesh.workflow.runtime import Step, WorkflowDefinition, WorkflowRuntime
from forgemesh.workflow.states import WorkflowStatus
from jsonschema import Draft202012Validator

#: 兼容性/conformance 测试统一标记（对齐 tests/compat 既有先例）。
_compat = pytest.mark.contract

#: 仓库根目录与插件根目录。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS_ROOT = _REPO_ROOT / "plugins"
_SCHEMA_PATH = _REPO_ROOT / "api" / "schemas" / "plugin-manifest.schema.json"

#: v1.1 阶段 E 纳入发布基线的 10 个插件目录名。
ALL_PLUGIN_DIRS: list[str] = [
    "decision-rules",
    "decision-jev",
    "decision-typesafe-jev",
    "workflow-reference",
    "workflow-temporal",
    "gpu-nvidia",
    "runtime-vllm",
    "runtime-tgi",
    "qos-basic",
    "metering-basic",
]

#: manifest 允许的隔离级别枚举（对齐 schema enum 与 ADR-006）。
_ALLOWED_ISOLATION = {"in-process", "restricted-rpc", "sandbox"}

#: 最小权限形式：至少两段点分的小写标识（如 ``resource.read``）。
_PERMISSION_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")

#: 10 插件 manifest schema（模块级加载一次，供第 1 节复用）。
_MANIFEST_SCHEMA: dict[str, Any] = json.loads(
    _SCHEMA_PATH.read_text(encoding="utf-8"),
)


# --------------------------------------------------------------------------- #
# 插件加载辅助（参照 tests/unit/plugins/conftest.py，tests/compat 目录无该 conftest）
# --------------------------------------------------------------------------- #


def _ensure_plugin_module(module_name: str, plugin_dir: str) -> None:
    """把 ``plugins/<plugin_dir>/provider.py`` 加载为模块并注册到 sys.modules。

    与 tests/unit/plugins/conftest.py 使用相同模块名，全量测试下两个目录
    共享同一模块实例；已注册时幂等跳过。
    """
    if module_name in sys.modules:
        return
    path = _PLUGINS_ROOT / plugin_dir / "provider.py"
    if not path.is_file():
        raise ImportError(f"plugin provider not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)


def _ensure_plugin_package(package_name: str, plugin_dir: str) -> None:
    """把 ``plugins/<plugin_dir>/`` 作为 Python 包加载并注册到 sys.modules。

    通过设置 ``submodule_search_locations`` 使子模块（provider/deploy 等）
    可正常导入；已注册时幂等跳过。
    """
    if package_name in sys.modules:
        return
    pkg_path = _PLUGINS_ROOT / plugin_dir
    init_path = pkg_path / "__init__.py"
    if not init_path.is_file():
        raise ImportError(f"plugin package __init__ not found: {init_path}")
    spec = importlib.util.spec_from_file_location(
        package_name, init_path, submodule_search_locations=[str(pkg_path)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {init_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


def _load_manifest_dict(plugin_dir: str) -> dict[str, Any]:
    """读取插件 manifest.yaml 并 yaml.safe_load 为 dict。"""
    path = _PLUGINS_ROOT / plugin_dir / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"manifest 应为映射：{path}"
    return data


def _run(coro: Awaitable[Any]) -> Any:
    """驱动协程到完成（每次独立事件循环，避免循环复用告警）。"""
    return asyncio.run(coro)


# =========================================================================== #
# 第 1 节：manifest 校验 + isolation 声明一致性（参数化 10 插件）
# =========================================================================== #


class TestManifestAndIsolationConformance:
    """manifest schema 校验、isolation 声明与目录形态一致性、最小权限形式。"""

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [pytest.param(p, id=p) for p in ALL_PLUGIN_DIRS],
    )
    def test_manifest_passes_json_schema(self, plugin_dir: str) -> None:
        """manifest.yaml 须通过 plugin-manifest.schema.json 校验（Draft 2020-12）。"""
        manifest = _load_manifest_dict(plugin_dir)
        validator = Draft202012Validator(_MANIFEST_SCHEMA)
        errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.path))
        assert not errors, (
            f"{plugin_dir}/manifest.yaml schema 校验失败: "
            + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        )

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [pytest.param(p, id=p) for p in ALL_PLUGIN_DIRS],
    )
    def test_manifest_parses_into_plugin_manifest_model(
        self, plugin_dir: str
    ) -> None:
        """manifest 须能加载进 PluginManifest 模型（extra=forbid，捕获模型与 schema 漂移）。"""
        manifest = PluginManifest.model_validate(_load_manifest_dict(plugin_dir))
        raw = _load_manifest_dict(plugin_dir)
        # 模型字段与原始声明一致（特别是 isolation 透传无丢失）。
        assert manifest.metadata.id == raw["metadata"]["id"]
        assert manifest.metadata.version == raw["metadata"]["version"]
        assert manifest.spec.isolation == raw["spec"]["isolation"]

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [pytest.param(p, id=p) for p in ALL_PLUGIN_DIRS],
    )
    def test_isolation_declaration_within_allowed_enum(
        self, plugin_dir: str
    ) -> None:
        """isolation 声明值须落在允许枚举 {in-process, restricted-rpc, sandbox} 内。"""
        isolation = _load_manifest_dict(plugin_dir)["spec"]["isolation"]
        assert isolation in _ALLOWED_ISOLATION, (
            f"{plugin_dir}: 非法 isolation 声明 {isolation!r}"
        )

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [pytest.param(p, id=p) for p in ALL_PLUGIN_DIRS],
    )
    def test_isolation_matches_directory_shape(self, plugin_dir: str) -> None:
        """isolation 声明须与目录形态一致。

        * 声明 ``in-process``：目录须有可加载入口（provider.py 或
          ``__init__.py``），且进程内实际加载成功；
        * 声明 ``restricted-rpc`` / ``sandbox``：允许仅有清单（不强求沙箱
          运行时存在），枚举合法性由前一用例保证。
        """
        manifest = _load_manifest_dict(plugin_dir)
        isolation = manifest["spec"]["isolation"]
        plugin_path = _PLUGINS_ROOT / plugin_dir

        if isolation == "in-process":
            has_entry = (plugin_path / "provider.py").is_file() or (
                plugin_path / "__init__.py"
            ).is_file()
            assert has_entry, (
                f"{plugin_dir} 声明 in-process 但缺少可加载入口"
                "（provider.py 或 __init__.py）"
            )
            # 进程内加载须成功（幂等注册到 sys.modules）。
            if (plugin_path / "provider.py").is_file():
                _ensure_plugin_module(
                    f"_conformance_{plugin_dir.replace('-', '_')}", plugin_dir
                )
            else:
                _ensure_plugin_package(
                    f"_conformance_{plugin_dir.replace('-', '_')}", plugin_dir
                )
        # restricted-rpc / sandbox：不强制沙箱运行时存在，无需目录形态断言。

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [pytest.param(p, id=p) for p in ALL_PLUGIN_DIRS],
    )
    def test_permissions_nonempty_and_parseable(self, plugin_dir: str) -> None:
        """spec.permissions 非空，且每项均为可解析的最小权限点分形式。"""
        permissions = _load_manifest_dict(plugin_dir)["spec"]["permissions"]
        assert permissions, f"{plugin_dir}: permissions 不得为空"
        for perm in permissions:
            assert isinstance(perm, str) and perm, (
                f"{plugin_dir}: 权限项须为非空字符串: {perm!r}"
            )
            assert _PERMISSION_RE.fullmatch(perm), (
                f"{plugin_dir}: 权限项 {perm!r} 不符合 '域.动作' 最小权限形式"
            )


# =========================================================================== #
# 第 2 节：Decision Provider 契约 conformance
# =========================================================================== #


def _make_rules_provider() -> Any:
    """构造 decision-rules 的 RulesProvider（注入 2 条规则：1 命中 1 不命中）。"""
    _ensure_plugin_module("decision_rules_provider", "decision-rules")
    from decision_rules_provider import Rule, RulesProvider

    return RulesProvider(
        rules=[
            Rule(
                name="prefer-a100",
                condition=lambda req: req.context.get("gpu") == "a100",
                action="inference.bind",
                parameters={"device": "a100"},
                rationale=["请求上下文指定 a100 且集群有空闲 a100"],
                confidence=0.8,
            ),
            Rule(
                name="never-match",
                condition=lambda req: False,
                action="inference.reject",
                rationale=["占位规则，永不命中"],
                confidence=0.1,
            ),
        ]
    )


def _make_jev_provider() -> Any:
    """构造 decision-jev 的 JEVProvider（注入复合 JEV 表达式规则）。"""
    _ensure_plugin_module("decision_jev_provider", "decision-jev")
    from decision_jev_provider import JEVProvider, JEVRule

    return JEVProvider(
        rules=[
            JEVRule(
                name="idle-gpu-first",
                expression={
                    "and": [
                        {"field": "gpu", "op": "eq", "value": "a100"},
                        {"field": "idle_ratio", "op": "gt", "value": 0.5},
                    ]
                },
                action="inference.bind-idle",
                parameters={"strategy": "idle-first"},
                rationale=["JEV: gpu==a100 且 idle_ratio>0.5"],
                confidence=0.7,
            )
        ]
    )


def _make_typesafe_provider(
    decisions: dict[str, Any] | None = None,
    *,
    error: Exception | None = None,
    delay_s: float = 0.0,
) -> Any:
    """构造 decision-typesafe-jev 的 TypeSafeJevProvider（注入离线 fake 客户端）。"""
    _ensure_plugin_module(
        "decision_typesafe_jev_provider", "decision-typesafe-jev"
    )
    from decision_typesafe_jev_provider import (
        JevQuestion,
        StaticJevClient,
        TypeSafeJevProvider,
    )

    if decisions is None:
        decisions = {"q1": {"choice": "bind.a100", "probability": 0.9}}
    client = StaticJevClient(decisions, error=error, delay_s=delay_s)
    return TypeSafeJevProvider(
        client=client,
        questions=[
            JevQuestion(
                key="q1",
                options=["bind.a100", "reject"],
                parameters={"device": "a100"},
                rationale=["空闲 a100 可承载该推理请求"],
                min_probability=0.5,
            )
        ],
        timeout_s=2.0,
    )


#: 决策 Provider 工厂表（键与插件目录名一致，供参数化）。
_DECISION_FACTORIES: dict[str, Callable[[], Any]] = {
    "decision-rules": _make_rules_provider,
    "decision-jev": _make_jev_provider,
    "decision-typesafe-jev": _make_typesafe_provider,
}


def _make_decision_request(**overrides: Any) -> Any:
    """构造契约合法的 DecisionRequest（默认上下文命中三个 Provider 的规则）。"""
    from forgemesh.decision.provider import DecisionRequest

    payload: dict[str, Any] = {
        "request_id": "req-conformance-1",
        "tenant_id": "tenant-alpha",
        "operation": "inference.schedule",
        "context": {"gpu": "a100", "idle_ratio": 0.8},
    }
    payload.update(overrides)
    return DecisionRequest(**payload)


class TestDecisionProviderConformance:
    """按 DecisionProvider 契约对 3 个决策 Provider 注入测试。

    契约要点（forgemesh.decision.provider）：decide 返回 0..N 个
    DecisionCandidate（candidate_id/action/rationale/confidence）；
    provider_id/provider_version 必须存在（IVT-11 决策记录）。
    """

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param(p, id=p)
            for p in ("decision-rules", "decision-jev", "decision-typesafe-jev")
        ],
    )
    def test_normal_input_returns_structured_candidates(
        self, plugin_dir: str
    ) -> None:
        """正常输入返回结构化候选：含建议动作（action）与理由（rationale）字段。"""
        provider = _DECISION_FACTORIES[plugin_dir]()
        candidates = _run(provider.decide(_make_decision_request()))

        assert len(candidates) == 1, "命中规则数应为 1"
        cand = candidates[0]
        # 按契约真实字段断言：建议动作 + 理由 + 置信度（仅排序/解释用）。
        assert cand.candidate_id, "candidate_id 不得为空"
        assert cand.action, "建议动作 action 不得为空"
        assert cand.action.startswith("inference.") or cand.action.startswith(
            "bind."
        ), f"动作应为推理域动作: {cand.action!r}"
        assert isinstance(cand.rationale, list) and cand.rationale, (
            "理由 rationale 应为非空列表（可解释性要求）"
        )
        assert cand.confidence is not None and 0.0 <= cand.confidence <= 1.0
        assert isinstance(cand.parameters, dict)

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param(p, id=p)
            for p in ("decision-rules", "decision-jev", "decision-typesafe-jev")
        ],
    )
    def test_provider_identity_declared_and_matches_manifest(
        self, plugin_dir: str
    ) -> None:
        """provider_id/provider_version 须声明（IVT-11）且与 manifest metadata.id 一致。"""
        provider = _DECISION_FACTORIES[plugin_dir]()
        assert provider.provider_id, "provider_id 不得为空（IVT-11 决策记录）"
        assert provider.provider_version, "provider_version 不得为空"
        manifest = _load_manifest_dict(plugin_dir)
        assert provider.provider_id == manifest["metadata"]["id"], (
            f"{plugin_dir}: provider_id={provider.provider_id!r} 与 manifest "
            f"id={manifest['metadata']['id']!r} 不一致"
        )

    @_compat
    @pytest.mark.parametrize(
        ("plugin_dir", "bad_payload", "reason"),
        [
            pytest.param(
                "decision-rules",
                {"tenant_id": "t", "operation": "op"},
                "缺 request_id",
                id="decision-rules-missing-request-id",
            ),
            pytest.param(
                "decision-jev",
                {"request_id": "r", "operation": "op"},
                "缺 tenant_id",
                id="decision-jev-missing-tenant-id",
            ),
            pytest.param(
                "decision-typesafe-jev",
                {"request_id": "r", "tenant_id": "t", "risk_level": "extreme"},
                "非法风险等级",
                id="decision-typesafe-jev-bad-risk-level",
            ),
            pytest.param(
                "decision-rules",
                {"request_id": "r", "tenant_id": "t", "deadline_ms": 0},
                "deadline_ms 低于下界",
                id="decision-rules-zero-deadline",
            ),
            pytest.param(
                "decision-jev",
                {"request_id": "r", "tenant_id": "t", "extra_field": 1},
                "extra=forbid 禁止未知字段",
                id="decision-jev-extra-field",
            ),
        ],
    )
    def test_invalid_input_rejected_not_silent_success(
        self, plugin_dir: str, bad_payload: dict[str, Any], reason: str
    ) -> None:
        """无效输入（缺字段/坏类型/越界）须在校验层抛 ValidationError，不静默成功。"""
        from forgemesh.decision.provider import DecisionRequest
        from pydantic import ValidationError

        provider = _DECISION_FACTORIES[plugin_dir]()
        # 非法输入在构造请求时即被拒（extra=forbid + 字段约束），
        # 根本到不了 Provider 内部——不存在静默成功的路径。
        with pytest.raises(ValidationError):
            DecisionRequest(**bad_payload)
        # 佐证：合法请求仍可正常决策（Provider 未被非法输入破坏）。
        assert _run(provider.decide(_make_decision_request()))

    @_compat
    def test_typesafe_jev_invalid_output_raises_protocol_error(self) -> None:
        """typesafe-jev：Jev 返回 choice 越界/结构非法 → JevProtocolError（触发降级）。"""
        _ensure_plugin_module(
            "decision_typesafe_jev_provider", "decision-typesafe-jev"
        )
        from decision_typesafe_jev_provider import JevProtocolError

        # choice 不在预定义 options 内 → 类型安全校验失败。
        provider = _make_typesafe_provider(
            decisions={"q1": {"choice": "teleport", "probability": 0.9}}
        )
        with pytest.raises(JevProtocolError, match="not in options"):
            _run(provider.decide(_make_decision_request()))

        # 决策结构非法（非对象）→ 协议错误，不静默成功。
        provider2 = _make_typesafe_provider(decisions={"q1": "not-a-dict"})
        with pytest.raises(JevProtocolError, match="invalid decision"):
            _run(provider2.decide(_make_decision_request()))

    @_compat
    def test_typesafe_jev_unavailable_dependency_raises_for_degrade(self) -> None:
        """typesafe-jev：依赖故障 → JevUnavailableError，绝不默认放行。"""
        _ensure_plugin_module(
            "decision_typesafe_jev_provider", "decision-typesafe-jev"
        )
        from decision_typesafe_jev_provider import JevUnavailableError

        provider = _make_typesafe_provider(error=RuntimeError("backend down"))
        with pytest.raises(JevUnavailableError, match="call failed"):
            _run(provider.decide(_make_decision_request()))

    @_compat
    def test_jev_invalid_expression_raises_value_error(self) -> None:
        """decision-jev：非法 JEV 表达式（未知操作符）→ ValueError，不静默成功。"""
        _ensure_plugin_module("decision_jev_provider", "decision-jev")
        from decision_jev_provider import JEVProvider, JEVRule

        bad = JEVProvider(
            rules=[
                JEVRule(
                    name="bad-op",
                    expression={"field": "x", "op": "like", "value": 1},
                    action="inference.none",
                )
            ]
        )
        with pytest.raises(ValueError, match="unknown JEV operator"):
            _run(bad.decide(_make_decision_request()))

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param(p, id=p)
            for p in ("decision-rules", "decision-jev")
        ],
    )
    def test_local_providers_complete_within_deadline(self, plugin_dir: str) -> None:
        """rules/jev 为纯本地计算：decide 须在 deadline_ms 内完成（不超时）。"""
        provider = _DECISION_FACTORIES[plugin_dir]()
        req = _make_decision_request(deadline_ms=500)
        # 外部视角 conformance：调用方按契约 deadline 包裹时不得超时。
        result = _run(asyncio.wait_for(provider.decide(req), timeout=0.5))
        assert list(result), "本地 Provider 应即时返回命中候选"

    @_compat
    def test_typesafe_jev_deadline_timeout_raises(self) -> None:
        """typesafe-jev 超时语义：慢依赖 + 极小 deadline → JevTimeoutError 失败。"""
        _ensure_plugin_module(
            "decision_typesafe_jev_provider", "decision-typesafe-jev"
        )
        from decision_typesafe_jev_provider import JevTimeoutError

        # 注入永远慢于 deadline 的依赖（5s 延迟），deadline 仅 80ms；
        # Provider 内部 asyncio.wait_for 取消慢任务并抛超时，测试总耗时 ~80ms。
        provider = _make_typesafe_provider(delay_s=5.0)
        req = _make_decision_request(deadline_ms=80)
        with pytest.raises(JevTimeoutError, match="timed out"):
            _run(provider.decide(req))

    @_compat
    def test_rules_provider_empty_candidates_is_contractual(self) -> None:
        """decision-rules：无匹配返回空列表是契约允许的 0..N（由 Router 降级）。"""
        _ensure_plugin_module("decision_rules_provider", "decision-rules")
        from decision_rules_provider import RulesProvider

        provider = RulesProvider(rules=[])
        assert _run(provider.decide(_make_decision_request())) == []


# =========================================================================== #
# 第 3 节：Workflow Provider 契约 conformance
# =========================================================================== #


def _make_workflow_definition() -> WorkflowDefinition:
    """构造两步骤工作流定义（无依赖，测试用）。"""
    return WorkflowDefinition(
        name="conformance-flow",
        timeout_seconds=60,
        steps=[
            Step(id="s1", uses="test.provision"),
            Step(id="s2", uses="test.verify"),
        ],
    )


def _make_reference_runtime() -> WorkflowRuntime:
    """组装 workflow-reference 插件的 ReferenceWorkflowRuntime（内存存储）。

    步骤 handler 一直阻塞，运行在被取消前保持 ``Running``（与只记录状态、不执行步骤的
    temporal 内存客户端可比）。
    """
    _ensure_plugin_module("workflow_reference_provider", "workflow-reference")
    from forgemesh.workflowruntime.approval import ApprovalGate
    from forgemesh.workflowruntime.compensate import CompensationExecutor
    from forgemesh.workflowruntime.exec import WorkflowExecutor
    from forgemesh.workflowruntime.store import WorkflowStore
    from workflow_reference_provider import ReferenceWorkflowRuntime

    async def handler(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"step_id": step.id, "status": "ok"}

    return ReferenceWorkflowRuntime(
        store=WorkflowStore(),
        executor=WorkflowExecutor(),
        compensation=CompensationExecutor(),
        approval=ApprovalGate(),
        handler=handler,
    )


def _make_temporal_runtime() -> WorkflowRuntime:
    """组装 workflow-temporal 插件的 TemporalWorkflowProvider（内存客户端）。"""
    _ensure_plugin_package("workflow_temporal_plugin", "workflow-temporal")
    from workflow_temporal_plugin.provider import (
        InMemoryTemporalClient,
        TemporalWorkflowProvider,
    )

    return TemporalWorkflowProvider(client=InMemoryTemporalClient())


_WORKFLOW_FACTORIES: dict[str, Callable[[], WorkflowRuntime]] = {
    "workflow-reference": _make_reference_runtime,
    "workflow-temporal": _make_temporal_runtime,
}


def _run_workflow_scenario(
    plugin_dir: str, scenario: Callable[[WorkflowRuntime], Awaitable[None]]
) -> None:
    """在单个事件循环中对插件的运行时执行场景。

    运行在后台执行的实现（workflow-reference）必须在同一事件循环中使用；实现提供
    ``aclose`` 时最后调用它，不遗留后台任务。
    """
    runtime = _WORKFLOW_FACTORIES[plugin_dir]()

    async def main() -> None:
        try:
            await scenario(runtime)
        finally:
            aclose = getattr(runtime, "aclose", None)
            if aclose is not None:
                await aclose()

    _run(main())


class TestWorkflowProviderConformance:
    """按 WorkflowRuntime 契约对 2 个工作流 Provider 注入测试。

    契约要点（forgemesh.workflow.runtime）：start 返回 RunRef 且幂等；
    get_status / get_run 查询状态；cancel 后状态终结（状态机终态）；未知 run_id
    抛 NotFoundError（引擎映射为实现内统一异常）。每个场景在单个事件循环中执行。
    """

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param("workflow-reference", id="workflow-reference"),
            pytest.param("workflow-temporal", id="workflow-temporal"),
        ],
    )
    def test_start_then_get_status_queryable(self, plugin_dir: str) -> None:
        """start → get_status / get_run 可查询：两实现 start 后均为 Running。"""

        async def scenario(runtime: WorkflowRuntime) -> None:
            ref = await runtime.start(_make_workflow_definition(), {"model": "m"}, "key-status")

            assert ref.run_id, "RunRef.run_id 不得为空"
            assert ref.definition_version >= 1, "版本固定字段应为正整数"
            status = await runtime.get_status(ref.run_id)
            assert status == WorkflowStatus.RUNNING, (
                f"{plugin_dir}: start 后状态应为 Running，实际 {status}"
            )
            info = await runtime.get_run(ref.run_id)
            assert (info.run_id, info.status) == (ref.run_id, WorkflowStatus.RUNNING)
            assert not info.terminal

        _run_workflow_scenario(plugin_dir, scenario)

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param("workflow-reference", id="workflow-reference"),
            pytest.param("workflow-temporal", id="workflow-temporal"),
        ],
    )
    def test_cancel_reaches_terminal_state(self, plugin_dir: str) -> None:
        """cancel 后状态终结为 Cancelled（契约状态机终态，无出边）。"""

        async def scenario(runtime: WorkflowRuntime) -> None:
            ref = await runtime.start(_make_workflow_definition(), {}, "key-cancel")

            await runtime.cancel(ref.run_id, cleanup=False)

            assert await runtime.get_status(ref.run_id) == WorkflowStatus.CANCELLED
            assert (await runtime.get_run(ref.run_id)).terminal

        _run_workflow_scenario(plugin_dir, scenario)
        # 终态断言：CANCELLED 在契约状态机中无出边（不可再转移）。
        from forgemesh.workflow.states import WORKFLOW_TRANSITIONS

        assert WorkflowStatus.CANCELLED not in WORKFLOW_TRANSITIONS or (
            not WORKFLOW_TRANSITIONS[WorkflowStatus.CANCELLED]
        ), "Cancelled 应为终态（无出边）"

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param("workflow-reference", id="workflow-reference"),
            pytest.param("workflow-temporal", id="workflow-temporal"),
        ],
    )
    def test_idempotency_key_reuses_same_run(self, plugin_dir: str) -> None:
        """幂等键：同 idempotency_key 重复 start 返回同一 run（不重复建副作用）。"""

        async def scenario(runtime: WorkflowRuntime) -> None:
            defn = _make_workflow_definition()
            ref1 = await runtime.start(defn, {}, "key-idem")
            ref2 = await runtime.start(defn, {}, "key-idem")

            assert ref1.run_id == ref2.run_id, (
                f"{plugin_dir}: 同幂等键应复用同一 run，got {ref1.run_id} / {ref2.run_id}"
            )
            # 不同幂等键应产生不同 run。
            ref3 = await runtime.start(defn, {}, "key-idem-other")
            assert ref3.run_id != ref1.run_id

        _run_workflow_scenario(plugin_dir, scenario)

    @_compat
    @pytest.mark.parametrize(
        "plugin_dir",
        [
            pytest.param("workflow-reference", id="workflow-reference"),
            pytest.param("workflow-temporal", id="workflow-temporal"),
        ],
    )
    def test_unknown_run_raises_not_found(self, plugin_dir: str) -> None:
        """未知 run_id 查询抛契约定义的 NotFoundError（不静默返回默认状态）。"""

        async def scenario(runtime: WorkflowRuntime) -> None:
            with pytest.raises(NotFoundError):
                await runtime.get_status("run-does-not-exist")
            with pytest.raises(NotFoundError):
                await runtime.get_run("run-does-not-exist")

        _run_workflow_scenario(plugin_dir, scenario)

    @_compat
    def test_temporal_cancel_cleanup_runs_compensations_in_reverse(self) -> None:
        """temporal 特有：cancel(cleanup=True) 按定义逆序执行已注册补偿。"""
        _ensure_plugin_package("workflow_temporal_plugin", "workflow-temporal")
        from workflow_temporal_plugin.provider import (
            InMemoryTemporalClient,
            TemporalWorkflowProvider,
        )

        provider = TemporalWorkflowProvider(client=InMemoryTemporalClient())
        executed: list[str] = []

        async def comp_s1(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
            executed.append(step.id)
            return {}

        async def comp_s2(step: Step, inputs: dict[str, Any]) -> dict[str, Any]:
            executed.append(step.id)
            return {}

        provider.register_compensation("s1", comp_s1)
        provider.register_compensation("s2", comp_s2)

        ref = _run(provider.start(_make_workflow_definition(), {}, "key-comp"))
        _run(provider.cancel(ref.run_id, cleanup=True))

        assert executed == ["s2", "s1"], "补偿应按定义逆序执行（反向回滚）"
        assert _run(provider.get_status(ref.run_id)) == WorkflowStatus.CANCELLED


# =========================================================================== #
# 第 4 节：Runtime Provider 契约 conformance（runtime-vllm / runtime-tgi）
# =========================================================================== #


class TestRuntimeVllmConformance:
    """runtime-vllm：deploy → verify → register_route(Ready) → release 闭环。

    构造方式对齐既有 tests/unit/plugins/test_runtime_vllm.py：launcher、
    http_client、租约释放与分配核验均注入 mock，不起真实进程。
    """

    @pytest.fixture(autouse=True)
    def _register_plugin_package(self) -> None:
        """setup 阶段注册插件包，使测试方法内的 provider 导入可用。"""
        _ensure_plugin_package("runtime_vllm_plugin", "runtime-vllm")

    @staticmethod
    def _make_deployer() -> Any:
        """构造注入 mock launcher 的 VLLMDeployer。"""
        _ensure_plugin_package("runtime_vllm_plugin", "runtime-vllm")
        from runtime_vllm_plugin.deploy import VLLMDeployer

        async def launcher(config: Any, instance_id: str) -> str:
            return f"http://127.0.0.1:{config.port}"

        return VLLMDeployer(launcher=launcher)

    @staticmethod
    def _make_verifier(ok: bool) -> Any:
        """构造注入 mock http 的 ServiceVerifier（ok 控制推理是否成功）。"""
        _ensure_plugin_package("runtime_vllm_plugin", "runtime-vllm")
        from runtime_vllm_plugin.verify import ServiceVerifier

        async def http(endpoint: str, prompt: str) -> dict[str, Any]:
            return {
                "ok": ok,
                "latency_ms": 12.5,
                "text": "hi" if ok else "",
                "status": 200 if ok else 503,
            }

        return ServiceVerifier(http_client=http)

    @staticmethod
    def _make_releaser() -> Any:
        """构造注入 mock 租约释放/分配核验的 ResourceReleaser（恒成功）。"""
        _ensure_plugin_package("runtime_vllm_plugin", "runtime-vllm")
        from runtime_vllm_plugin.release import ResourceReleaser

        async def release_lease(lease_id: str) -> bool:
            return True

        async def verify_allocation(device_ids: list[int]) -> bool:
            return True

        return ResourceReleaser(
            lease_releaser=release_lease,
            allocation_verifier=verify_allocation,
        )

    @_compat
    def test_deploy_verify_release_loop(self) -> None:
        """闭环：deploy(Running) → verify 通过 → register_route(Ready) → release 成功。"""
        from runtime_vllm_plugin.deploy import DeployConfig, InstanceStatus
        from runtime_vllm_plugin.verify import VerifyConfig

        deployer = self._make_deployer()
        verifier = self._make_verifier(ok=True)
        releaser = self._make_releaser()

        # 1. deploy：实例 Running 且 endpoint 非空。
        instance = _run(deployer.deploy(DeployConfig(model_path="/m", port=8100, gpu_ids=[0])))
        assert instance.endpoint, "deploy 后 endpoint 应非空"
        stored = _run(deployer.get_instance(instance.instance_id))
        assert stored is not None and stored.status == InstanceStatus.RUNNING

        # 2. verify：冒烟通过；通过后注册路由推进 Ready。
        result = _run(
            verifier.verify(VerifyConfig(endpoint=instance.endpoint, num_requests=2))
        )
        assert result.passed is True
        endpoint = _run(deployer.register_route(instance))
        assert endpoint == instance.endpoint
        ready = _run(deployer.get_instance(instance.instance_id))
        assert ready is not None and ready.status == InstanceStatus.READY

        # 3. release：停止 + 租约释放 + 分配核验全部成功。
        release = _run(
            releaser.release(deployer, instance.instance_id, "lease-1", [0])
        )
        assert release.released is True
        assert release.verified is True
        stopped = _run(deployer.get_instance(instance.instance_id))
        assert stopped is not None and stopped.status == InstanceStatus.STOPPED

    @_compat
    def test_verify_failure_does_not_mark_ready(self) -> None:
        """verify 失败：实例不得标记 Ready（保持在 Running，不得进入路由）。"""
        from runtime_vllm_plugin.deploy import DeployConfig, InstanceStatus
        from runtime_vllm_plugin.verify import VerifyConfig

        deployer = self._make_deployer()
        verifier = self._make_verifier(ok=False)  # 推理全部失败。

        instance = _run(deployer.deploy(DeployConfig(model_path="/m", port=8101, gpu_ids=[0])))
        result = _run(
            verifier.verify(VerifyConfig(endpoint=instance.endpoint, num_requests=2))
        )
        assert result.passed is False, "推理失败时 verify 不得通过"
        # conformance：verify 失败后不得推进 Ready（register_route 不应被调用）。
        stored = _run(deployer.get_instance(instance.instance_id))
        assert stored is not None
        assert stored.status != InstanceStatus.READY, (
            "verify 失败的实例不得标记 Ready（不得进入服务路由）"
        )
        assert stored.status == InstanceStatus.RUNNING

    @_compat
    def test_release_idempotent(self) -> None:
        """release 幂等：重复 release 同一实例不抛错且结果保持成功（实现语义）。"""
        from runtime_vllm_plugin.deploy import DeployConfig

        deployer = self._make_deployer()
        releaser = self._make_releaser()
        instance = _run(deployer.deploy(DeployConfig(model_path="/m", port=8102, gpu_ids=[0])))

        first = _run(
            releaser.release(deployer, instance.instance_id, "lease-2", [0])
        )
        # 第二次 release：stop_instance 对已停止实例幂等返回，不抛错。
        second = _run(
            releaser.release(deployer, instance.instance_id, "lease-2", [0])
        )
        assert first.released is True
        assert second.released is True, "重复 release 应幂等成功（实现语义）"

    @_compat
    def test_release_unknown_instance_reported_not_raised(self) -> None:
        """release 未知实例：按实现语义返回 released=False（不抛错、不虚报成功）。"""
        deployer = self._make_deployer()
        releaser = self._make_releaser()
        result = _run(releaser.release(deployer, "vllm-unknown", "lease-x", []))
        assert result.released is False
        assert "not found" in result.details


class TestRuntimeTgiConformance:
    """runtime-tgi：deploy → verify → release 闭环（与 runtime-vllm 行为一致）。

    构造方式对齐既有 tests/unit/plugins/test_runtime_tgi.py：launcher、
    http_client、租约释放与分配核验均注入 mock。
    """

    @pytest.fixture(autouse=True)
    def _register_plugin_package(self) -> None:
        """setup 阶段注册插件包，使测试方法内的 provider 导入可用。"""
        _ensure_plugin_package("runtime_tgi_plugin", "runtime-tgi")

    @staticmethod
    def _make_provider(
        *,
        ok: bool = True,
        http_error: Exception | None = None,
    ) -> Any:
        """构造注入全部 mock 依赖的 TgiRuntimeProvider。"""
        _ensure_plugin_package("runtime_tgi_plugin", "runtime-tgi")
        from runtime_tgi_plugin.provider import TgiRuntimeProvider

        async def launcher(config: Any, instance_id: str) -> str:
            return f"http://127.0.0.1:{config.port}"

        async def http(endpoint: str, prompt: str) -> dict[str, Any]:
            if http_error is not None:
                raise http_error
            return {
                "ok": ok,
                "latency_ms": 20.0,
                "text": "hi" if ok else "",
                "status": 200 if ok else 503,
            }

        async def release_lease(lease_id: str) -> bool:
            return True

        async def verify_allocation(device_ids: list[int]) -> bool:
            return True

        return TgiRuntimeProvider(
            launcher=launcher,
            http_client=http,
            lease_releaser=release_lease,
            allocation_verifier=verify_allocation,
        )

    @staticmethod
    def _deploy(provider: Any) -> Any:
        """部署一个测试实例（含资源分配声明）。"""
        from runtime_tgi_plugin.provider import ResourceAllocation, TgiDeployConfig

        return _run(
            provider.deploy(
                TgiDeployConfig(model_id="m/Llama-3-8B", port=8080, gpu_ids=[0]),
                ResourceAllocation(lease_id="lease-tgi", device_ids=[0]),
            )
        )

    @_compat
    def test_deploy_verify_release_loop(self) -> None:
        """闭环：deploy(Running) → verify 通过 → release 成功且实例 Stopped。"""
        from runtime_tgi_plugin.provider import InstanceStatus

        provider = self._make_provider(ok=True)

        instance = self._deploy(provider)
        assert instance.endpoint, "deploy 后 endpoint 应非空"
        stored = _run(provider.get_instance(instance.instance_id))
        assert stored is not None and stored.status == InstanceStatus.RUNNING

        result = _run(provider.verify(instance))
        assert result.passed is True

        _run(provider.release(instance))  # 全部 mock 成功，不应抛错。
        stopped = _run(provider.get_instance(instance.instance_id))
        assert stopped is not None and stopped.status == InstanceStatus.STOPPED

    @_compat
    def test_verify_failure_does_not_mark_ready(self) -> None:
        """verify 失败：实例不得标记 Ready（保持 Running；异常同样降级为失败）。"""
        from runtime_tgi_plugin.provider import InstanceStatus

        provider = self._make_provider(ok=False)
        instance = self._deploy(provider)
        result = _run(provider.verify(instance))
        assert result.passed is False
        stored = _run(provider.get_instance(instance.instance_id))
        assert stored is not None
        assert stored.status != InstanceStatus.READY, "verify 失败不得标记 Ready"
        assert stored.status == InstanceStatus.RUNNING

        # http 抛异常 → verify 降级为 passed=False（不向上抛）。
        provider2 = self._make_provider(http_error=RuntimeError("conn refused"))
        instance2 = self._deploy(provider2)
        result2 = _run(provider2.verify(instance2))
        assert result2.passed is False

    @_compat
    def test_release_idempotent(self) -> None:
        """release 幂等：重复 release 同一实例不抛错（实现语义：流程可重复执行）。"""
        provider = self._make_provider(ok=True)
        instance = self._deploy(provider)

        _run(provider.release(instance))
        # 第二次 release：实例记录仍在，mock 全成功 → 不抛错。
        _run(provider.release(instance))
        stored = _run(provider.get_instance(instance.instance_id))
        assert stored is not None

    @_compat
    def test_release_unknown_instance_raises(self) -> None:
        """release 未知实例：按实现语义抛 ValueError（不静默成功）。"""
        from runtime_tgi_plugin.provider import ResourceAllocation, TgiDeployConfig, TgiInstance

        provider = self._make_provider()
        ghost = TgiInstance(
            instance_id="tgi-ghost",
            config=TgiDeployConfig(model_id="m/x", port=8081, gpu_ids=[]),
            allocation=ResourceAllocation(lease_id="lease-ghost"),
        )
        with pytest.raises(ValueError, match="instance not found"):
            _run(provider.release(ghost))


# =========================================================================== #
# 第 5 节：升级/卸载安全
# =========================================================================== #


def _manifest_model(plugin_dir: str, *, version: str | None = None) -> PluginManifest:
    """加载插件 manifest.yaml 为 PluginManifest 模型（可覆盖 version）。"""
    data = _load_manifest_dict(plugin_dir)
    if version is not None:
        data = {**data, "metadata": {**data["metadata"], "version": version}}
    return PluginManifest.model_validate(data)


class TestUpgradeAndUnloadSafety:
    """升级/卸载安全：运行中引用下的卸载语义与版本并存规则。

    【DEFECT-E1-01 备案】当前 ``PluginManager``（control-plane
    plugin_manager.py）：

    * ``PLUGIN_TRANSITIONS`` 允许 ``ENABLED → UNINSTALLED`` 直接转移；
    * 无运行中引用计数，也无 ``Draining`` 状态（6 态生命周期中不存在）。

    即：能力正被运行时持有（如 Router 已通过 ``get_capabilities`` 取得
    能力句柄并在途调度）的插件可被立即卸载。任务要求"运行中被引用的
    Provider 不可无序卸载"，该要求**当前未被实现满足**——本节按实现
    真实语义断言当前行为，并在此与 docstring 记录期望行为：

    * 期望：卸载在途引用非零的插件应被拒绝（ConflictError/
      PreconditionFailedError），或先进入 Draining 状态，待引用归零后
      再完成 Uninstalled。
    """

    @_compat
    def test_enabled_plugin_with_live_reference_uninstallable_current_behavior(
        self,
    ) -> None:
        """【当前行为断言 + 缺陷备案】运行中被引用的 Enabled 插件可被直接卸载。

        期望行为（未实现）：应被拒绝或进入 Draining，见类 docstring。
        """
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        pid = manager.register(_manifest_model("decision-rules"))

        # 走完合法安装链路：Discovered → Validating → Installed → Enabled。
        for target in (
            PluginLifecycleState.VALIDATING,
            PluginLifecycleState.INSTALLED,
            PluginLifecycleState.ENABLED,
        ):
            manager.transition(pid, target)

        # 模拟运行中引用：Router/调度器持有该插件的能力句柄并在途使用。
        live_capabilities = manager.get_capabilities(pid)
        assert live_capabilities, "Enabled 插件应暴露能力（模拟在途引用）"
        assert manager.get_health(pid) == "Unknown"

        # 当前实现：无视在途引用，直接卸载成功（DEFECT-E1-01）。
        record = manager.transition(pid, PluginLifecycleState.UNINSTALLED)
        assert record.lifecycle_state == PluginLifecycleState.UNINSTALLED
        # 卸载后记录仍可读（历史可审计），但已不在 installed 列表。
        assert manager.list_installed() == []

    @_compat
    def test_uninstall_unknown_plugin_rejected(self) -> None:
        """卸载未注册插件须被拒绝（NotFoundError）。"""
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        with pytest.raises(NotFoundError):
            manager.transition(
                "forgemesh.ghost:0.0.1", PluginLifecycleState.UNINSTALLED
            )

    @_compat
    def test_disorderly_transition_rejected(self) -> None:
        """无序转移被拒：Discovered 不能直接 Enabled/Uninstalled 之外的越级跳转。"""
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        pid = manager.register(_manifest_model("qos-basic"))

        # 越级：Discovered → Enabled（须先 Validating → Installed）。
        with pytest.raises(InvalidStateTransition):
            manager.transition(pid, PluginLifecycleState.ENABLED)
        # 校验失败路径的合法卸载（Validating → Uninstalled）不受影响。
        manager.transition(pid, PluginLifecycleState.VALIDATING)
        manager.transition(pid, PluginLifecycleState.UNINSTALLED)

    @_compat
    def test_uninstalled_is_terminal_no_revive(self) -> None:
        """Uninstalled 为终态：已卸载插件不可再转移（不可复活/重启用）。"""
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        pid = manager.register(_manifest_model("metering-basic"))
        manager.transition(pid, PluginLifecycleState.VALIDATING)
        manager.transition(pid, PluginLifecycleState.UNINSTALLED)

        with pytest.raises(InvalidStateTransition):
            manager.transition(pid, PluginLifecycleState.ENABLED)

    @_compat
    def test_same_id_different_versions_coexist(self) -> None:
        """版本并存（ADR-011）：同 id 不同版本以 id:version 复合键并存。"""
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        m_v1 = _manifest_model("decision-rules", version="0.0.1")
        m_v2 = _manifest_model("decision-rules", version="0.1.0")

        pid1 = manager.register(m_v1)
        pid2 = manager.register(m_v2)

        assert pid1 != pid2, "不同版本应有不同 plugin_id（id:version）"
        same_id = [
            m
            for m in manager.discover()
            if m.metadata.id == "forgemesh.decision.rules"
        ]
        assert len(same_id) == 2, "同 id 两版本应同时可被发现（并存）"
        assert {manager.get_version(pid1), manager.get_version(pid2)} == {
            "0.0.1",
            "0.1.0",
        }

    @_compat
    def test_duplicate_exact_version_registration_conflicts(self) -> None:
        """重复注册同一 id:version 须抛 ConflictError（并存≠重复注册）。"""
        from forgemesh.controlplane.plugin_manager import PluginManager

        manager = PluginManager()
        manager.register(_manifest_model("workflow-reference"))
        with pytest.raises(ConflictError, match="already registered"):
            manager.register(_manifest_model("workflow-reference"))
