"""ForgeMesh 跨表示契约测试（T2）：Python 模型 vs JSON Schema vs 示例 YAML。

现有的 ``test_schema_validation.py`` 只校验「示例 YAML vs JSON Schema」，
本模块补齐「Python 模型 vs Schema / 示例」的一致性校验，覆盖以下核心模型：

* :class:`InferenceService` —— 资源包裹层 ``spec.resources.accelerator`` 结构；
* :class:`ObjectMeta` —— 多租户字段 snake_case / camelCase alias 双向一致；
* :class:`PluginManifest` / :class:`PluginSpec` —— 供应链安全字段、健康检查类型与权限词表；
* :class:`PolicyDecision` —— 三态（allow / deny / degraded）枚举；
* :class:`DecisionRequest` —— 风险等级与决策时限字段；
* :class:`Step` —— 补偿、审批与 ``with`` alias 字段；
* :class:`GovernanceBypassError` —— 治理绕过异常存在性与继承关系。

运行：``pytest tests/contract/test_cross_representation.py -v``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from forgemesh.core.errors import ForgeMeshError, GovernanceBypassError
from forgemesh.core.objects import ObjectMeta
from forgemesh.decision.provider import DecisionRequest
from forgemesh.plugin.manifest import PluginSpec
from forgemesh.plugin.permissions import Permission
from forgemesh.policy.api import PolicyDecision
from forgemesh.resource.inference import (
    InferenceService,
)
from forgemesh.workflow.runtime import Step
from jsonschema import Draft202012Validator
from pydantic import ValidationError

#: 仓库根目录（tests/contract/test_cross_representation.py -> parents[2]）。
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "api" / "schemas"
EXAMPLES_DIR = REPO_ROOT / "examples"


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件。"""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 文件（单文档）。"""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ---------------------------------------------------------------------------
# 1. InferenceService 跨表示一致性
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_inference_service_cross_representation() -> None:
    """InferenceService：示例 YAML → Python 模型 → JSON Schema 三者一致。

    步骤：
      1. 从 ``examples/single-node/inference-service.yaml`` 加载 YAML；
      2. 用 Python 模型 ``InferenceService(**yaml_dict)`` 构造对象；
      3. ``model_dump(by_alias=True)`` 序列化回 dict；
      4. 用 ``Draft202012Validator`` 校验序列化后的 dict 符合 Schema；
      5. 验证 ``spec.resources.accelerator`` 结构正确（不是扁平 ``spec.accelerator``）。

    注：``Resource`` 基类的顶层 ``conditions`` / ``finalizers``（受控删除
    生命周期字段）不在 InferenceService Schema 顶层定义，序列化后剔除再校验。
    """
    yaml_dict = _load_yaml(EXAMPLES_DIR / "single-node" / "inference-service.yaml")

    # Python 模型构造
    obj = InferenceService(**yaml_dict)
    assert obj.kind == "InferenceService"
    assert obj.spec.resources.accelerator.type == "nvidia-gpu"

    # 序列化回 dict（排除 None 值，因 Schema 的 uid/resourceVersion 不接受 null）
    dumped = obj.model_dump(by_alias=True, exclude_none=True)
    # Resource 基类顶层 conditions / finalizers 不在 InferenceService Schema 顶层定义
    dumped.pop("conditions", None)
    dumped.pop("finalizers", None)

    # JSON Schema 校验
    schema = _load_json(SCHEMAS_DIR / "inference-service.schema.json")
    Draft202012Validator(schema).validate(dumped)

    # spec.resources.accelerator 结构正确（不是扁平 spec.accelerator）
    assert "resources" in dumped["spec"]
    assert "accelerator" in dumped["spec"]["resources"]
    assert "accelerator" not in dumped["spec"], (
        "accelerator 应嵌套在 spec.resources 下，而非扁平挂在 spec 顶层"
    )
    assert dumped["spec"]["resources"]["accelerator"]["type"] == "nvidia-gpu"
    assert dumped["spec"]["resources"]["accelerator"]["count"] == 2


# ---------------------------------------------------------------------------
# 2. ObjectMeta 多租户字段一致性
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_object_meta_tenant_id_alias() -> None:
    """ObjectMeta.tenant_id：snake_case 构造 → camelCase 序列化；alias 双向一致。"""
    # Python 风格 snake_case 构造
    meta = ObjectMeta(name="test", tenant_id="team-a")
    dumped = meta.model_dump(by_alias=True)
    assert dumped["tenantId"] == "team-a", "snake_case 构造后应序列化为 camelCase tenantId"

    # JSON 风格 camelCase 构造（通过 alias）
    meta_alias = ObjectMeta(name="test", **{"tenantId": "team-b"})
    assert meta_alias.tenant_id == "team-b", "camelCase alias 构造后属性值应正确"

    # 两种方式构造的对象 tenant_id 属性值应各自正确
    assert meta.tenant_id == "team-a"
    assert meta_alias.tenant_id == "team-b"


@pytest.mark.contract
def test_object_meta_observed_generation_alias() -> None:
    """ObjectMeta.observed_generation：snake_case / camelCase alias 双向一致。"""
    # Python 风格 snake_case 构造
    meta = ObjectMeta(name="test", observed_generation=5)
    dumped = meta.model_dump(by_alias=True)
    assert dumped["observedGeneration"] == 5, (
        "snake_case 构造后应序列化为 camelCase observedGeneration"
    )

    # JSON 风格 camelCase 构造（通过 alias）
    meta_alias = ObjectMeta(name="test", **{"observedGeneration": 7})
    assert meta_alias.observed_generation == 7, "camelCase alias 构造后属性值应正确"


# ---------------------------------------------------------------------------
# 3. PluginManifest 供应链安全字段一致性
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_plugin_spec_supply_chain_fields() -> None:
    """PluginSpec：供应链安全字段（isolation/entrypoint/artifactDigest/signature）一致。"""
    spec = PluginSpec(
        category="provider",
        isolation="sandbox",
        entrypoint="main",
        artifactDigest="sha256:abc",
        signature="cosign:xyz",
    )
    dumped = spec.model_dump(by_alias=True)
    assert dumped["isolation"] == "sandbox"
    assert dumped["entrypoint"] == "main"
    assert dumped["artifactDigest"] == "sha256:abc"
    assert dumped["signature"] == "cosign:xyz"
    # 所有供应链字段都在输出 dict 中
    for field in ("isolation", "entrypoint", "artifactDigest", "signature"):
        assert field in dumped, f"供应链字段 {field} 应出现在序列化输出中"


@pytest.mark.contract
def test_plugin_spec_health_check_timeout_is_int() -> None:
    """PluginSpec.healthCheck：timeoutMs 为 int，不是 str。"""
    spec = PluginSpec(
        category="provider",
        healthCheck={"mode": "rpc", "timeoutMs": 2000},
    )
    assert spec.healthCheck["mode"] == "rpc"
    assert spec.healthCheck["timeoutMs"] == 2000
    assert isinstance(spec.healthCheck["timeoutMs"], int), "timeoutMs 必须是 int 类型"


@pytest.mark.contract
def test_plugin_manifest_schema_has_supply_chain_fields() -> None:
    """plugin-manifest Schema 应声明供应链安全字段。"""
    schema = _load_json(SCHEMAS_DIR / "plugin-manifest.schema.json")
    spec_props = schema["properties"]["spec"]["properties"]
    for field in ("isolation", "entrypoint", "artifactDigest", "signature", "healthCheck"):
        assert field in spec_props, f"Schema spec 应声明字段 {field}"
    # healthCheck.timeoutMs 应为 integer
    hc = spec_props["healthCheck"]["properties"]
    assert hc["timeoutMs"]["type"] == "integer", "Schema 中 timeoutMs 应为 integer"


@pytest.mark.contract
def test_plugin_permissions_schema_matches_vocabulary() -> None:
    """Schema 的权限枚举与 Permission 词表逐项一致，且不允许重复。"""
    schema = _load_json(SCHEMAS_DIR / "plugin-manifest.schema.json")
    permissions = schema["properties"]["spec"]["properties"]["permissions"]
    assert permissions["items"]["enum"] == [p.value for p in Permission]
    assert permissions["uniqueItems"] is True


def _plugin_manifest_with_permissions(permissions: list[str]) -> dict[str, Any]:
    return {
        "apiVersion": "forgemesh.io/v1alpha1",
        "kind": "PluginManifest",
        "metadata": {"id": "forgemesh.policy.example", "version": "0.1.0"},
        "spec": {"category": "policy", "provides": [], "requires": [], "permissions": permissions},
    }


@pytest.mark.contract
@pytest.mark.parametrize(
    "permissions",
    [["admin"], ["metrics:read"], ["Resource.Read"], ["resource.read", "resource.read"]],
)
def test_plugin_permissions_rejected_by_model_and_schema(permissions: list[str]) -> None:
    """词表外或重复的权限，Python 模型与 Schema 都拒绝。"""
    validator = Draft202012Validator(_load_json(SCHEMAS_DIR / "plugin-manifest.schema.json"))
    assert not list(validator.iter_errors(_plugin_manifest_with_permissions(["resource.read"])))

    manifest = _plugin_manifest_with_permissions(permissions)
    errors = list(validator.iter_errors(manifest))
    assert errors
    assert all("permissions" in error.absolute_path for error in errors)
    with pytest.raises(ValidationError, match="permissions"):
        PluginSpec.model_validate(manifest["spec"])


# ---------------------------------------------------------------------------
# 4. PolicyDecision 三态一致性
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_policy_decision_three_states() -> None:
    """PolicyDecision：allow / deny / require_approval 三态均可构造。"""
    assert PolicyDecision(decision="allow").decision == "allow"
    assert PolicyDecision(decision="deny").decision == "deny"
    assert PolicyDecision(decision="require_approval").decision == "require_approval"


@pytest.mark.contract
def test_policy_decision_rejects_invalid_state() -> None:
    """PolicyDecision：非法状态（maybe/degraded）必须被拒绝。"""
    with pytest.raises(ValidationError):
        PolicyDecision(decision="maybe")
    # degraded（降级放行）已按 architecture 7.5 移除，不得再作为决策类型
    with pytest.raises(ValidationError):
        PolicyDecision(decision="degraded")


# ---------------------------------------------------------------------------
# 5. DecisionRequest 风险等级字段
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_decision_request_risk_and_deadline() -> None:
    """DecisionRequest：risk_level 与 deadline_ms 字段可构造且默认值正确。"""
    req = DecisionRequest(
        request_id="r1",
        tenant_id="t1",
        operation="op",
        risk_level="high",
        deadline_ms=1000,
    )
    assert req.risk_level == "high"
    assert req.deadline_ms == 1000
    # 强类型恢复（development-plan 5.5）：默认 low / 2000
    default_req = DecisionRequest(request_id="r2", tenant_id="t1", operation="op")
    assert default_req.risk_level == "low"
    assert default_req.deadline_ms == 2000
    # 非法风险等级必须被拒绝
    with pytest.raises(ValidationError):
        DecisionRequest(request_id="r3", tenant_id="t1", operation="op", risk_level="extreme")


# ---------------------------------------------------------------------------
# 6. Step 补偿与审批字段
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_step_compensate_and_approval_fields() -> None:
    """Step：compensate / requires_approval / timeout 字段可构造且值正确。"""
    step = Step(
        id="s1",
        uses="gpu.discovery",
        timeout=30,
        compensate="s1-rollback",
        requires_approval=True,
    )
    assert step.timeout == 30
    assert step.compensate == "s1-rollback"
    assert step.requires_approval is True


@pytest.mark.contract
def test_step_with_alias() -> None:
    """Step：``with`` alias 生效，``with_`` 属性可正确访问。"""
    step = Step(id="s1", uses="gpu.discovery", **{"with": {"key": "val"}})
    assert step.with_ == {"key": "val"}
    dumped = step.model_dump(by_alias=True)
    assert dumped["with"] == {"key": "val"}, "序列化后键应为 alias 'with'"


# ---------------------------------------------------------------------------
# 7. GovernanceBypassError 存在性
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_governance_bypass_error_exists() -> None:
    """GovernanceBypassError 存在、是 ForgeMeshError 子类、可构造。"""
    assert issubclass(GovernanceBypassError, ForgeMeshError), (
        "GovernanceBypassError 必须是 ForgeMeshError 的子类"
    )
    # 构造不抛异常
    err = GovernanceBypassError("test")
    assert err.message == "test"
