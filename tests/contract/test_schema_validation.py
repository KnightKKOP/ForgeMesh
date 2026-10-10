"""ForgeMesh 契约测试：JSON Schema 与示例一致性（阶段 0）。

使用 ``jsonschema``（Draft 2020-12）+ ``pyyaml`` 校验：

* ``examples/single-node/inference-service.yaml`` 符合
  ``api/schemas/inference-service.schema.json``；
* ``examples/single-node/plugin-vllm.yaml`` 符合
  ``api/schemas/plugin-manifest.schema.json``；
* 4 个 Schema 自身是合法的 Draft 2020-12 文档；
* 负向用例（非法 phase、缺失必填字段）会被正确拒绝。

运行：``pytest tests/contract``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

#: 仓库根目录（tests/contract/test_schema_validation.py -> parents[2]）。
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "api" / "schemas"
EXAMPLES_DIR = REPO_ROOT / "examples"

INFERENCE_SERVICE_SCHEMA = SCHEMAS_DIR / "inference-service.schema.json"
EVENT_SCHEMA = SCHEMAS_DIR / "event.schema.json"
PLUGIN_MANIFEST_SCHEMA = SCHEMAS_DIR / "plugin-manifest.schema.json"
DEVICE_PROFILE_SCHEMA = SCHEMAS_DIR / "device-profile.schema.json"

ALL_SCHEMA_PATHS = (
    INFERENCE_SERVICE_SCHEMA,
    EVENT_SCHEMA,
    PLUGIN_MANIFEST_SCHEMA,
    DEVICE_PROFILE_SCHEMA,
)

#: InferenceService 生命周期 11 态（与 resource-model.InferencePhase 一致）。
EXPECTED_PHASES = {
    "Pending",
    "Scheduling",
    "Preparing",
    "Deploying",
    "Verifying",
    "Ready",
    "Degraded",
    "Updating",
    "Terminating",
    "Failed",
    "Unknown",
}


def _load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 文件。"""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_yaml(path: Path) -> Any:
    """读取 YAML 文件（单文档）。"""
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.mark.contract
def test_inference_service_example_matches_schema() -> None:
    """示例 InferenceService 必须通过其 JSON Schema 校验。"""
    schema = _load_json(INFERENCE_SERVICE_SCHEMA)
    instance = _load_yaml(EXAMPLES_DIR / "single-node" / "inference-service.yaml")
    Draft202012Validator(schema).validate(instance)


@pytest.mark.contract
def test_plugin_manifest_example_matches_schema() -> None:
    """示例 PluginManifest 必须通过其 JSON Schema 校验。"""
    schema = _load_json(PLUGIN_MANIFEST_SCHEMA)
    instance = _load_yaml(EXAMPLES_DIR / "single-node" / "plugin-vllm.yaml")
    Draft202012Validator(schema).validate(instance)


@pytest.mark.contract
@pytest.mark.parametrize("schema_path", ALL_SCHEMA_PATHS, ids=lambda p: p.name)
def test_schemas_are_valid_draft_2020_12(schema_path: Path) -> None:
    """每个 Schema 自身必须是合法的 Draft 2020-12 文档。"""
    schema = _load_json(schema_path)
    assert schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


@pytest.mark.contract
def test_phase_enum_has_eleven_states() -> None:
    """InferenceService status.phase 枚举必须恰好为 11 态。"""
    schema = _load_json(INFERENCE_SERVICE_SCHEMA)
    phases = schema["$defs"]["InferenceServiceStatus"]["properties"]["phase"]["enum"]
    assert len(phases) == 11
    assert set(phases) == EXPECTED_PHASES


@pytest.mark.contract
def test_inference_service_rejects_unknown_phase() -> None:
    """非法 phase 必须被拒绝。"""
    schema = _load_json(INFERENCE_SERVICE_SCHEMA)
    instance = _load_yaml(EXAMPLES_DIR / "single-node" / "inference-service.yaml")
    instance["status"]["phase"] = "NotARealPhase"
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(instance)


@pytest.mark.contract
def test_inference_service_rejects_missing_required_field() -> None:
    """缺失必填字段（spec.runtime）必须被拒绝。"""
    schema = _load_json(INFERENCE_SERVICE_SCHEMA)
    instance = _load_yaml(EXAMPLES_DIR / "single-node" / "inference-service.yaml")
    del instance["spec"]["runtime"]
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(instance)


@pytest.mark.contract
def test_event_schema_requires_producer_identity() -> None:
    """事件缺少 producer.component 时必须被拒绝。"""
    schema = _load_json(EVENT_SCHEMA)
    event = {
        "eventId": "evt-1",
        "eventType": "inference.service.ready",
        "schemaVersion": "v1",
        "occurredAt": "2026-10-09T12:00:00Z",
        "tenantId": "team-a",
        "resourceId": "llama-service",
        "correlationId": "corr-1",
        "producer": {"version": "1.0.0"},
        "data": {"phase": "Ready"},
    }
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(event)
