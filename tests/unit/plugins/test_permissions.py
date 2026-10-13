"""插件权限词表与清单权限校验单元测试。

覆盖：

* 词表取值形如 ``<域>.<动作>``，且与成员名一一对应；
* PluginSpec 只接受词表中的权限，拒绝旧版自由命名、大小写变体与重复项；
* 仓库内全部插件清单（``plugins/`` 与 ``examples/``）都满足词表约束。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from forgemesh.plugin.manifest import PluginManifest, PluginSpec
from forgemesh.plugin.permissions import Permission
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _shipped_manifests() -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for pattern in ("plugins/*/manifest.yaml", "examples/**/*.yaml"):
        for path in sorted(_REPO_ROOT.glob(pattern)):
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if isinstance(doc, dict) and doc.get("kind") == "PluginManifest":
                    found.append((str(path.relative_to(_REPO_ROOT)), doc))
    return found


_SHIPPED_MANIFESTS = _shipped_manifests()


class TestVocabulary:
    """词表本身的形式约束。"""

    def test_values_are_dotted_and_match_member_names(self) -> None:
        for permission in Permission:
            assert re.fullmatch(r"[a-z]+\.[a-z]+", permission.value)
            assert permission.name == permission.value.replace(".", "_").upper()

    def test_values_are_unique(self) -> None:
        values = [p.value for p in Permission]
        assert len(values) == len(set(values))


class TestPluginSpecPermissions:
    """清单 ``spec.permissions`` 的取值校验。"""

    def test_vocabulary_names_parsed_as_permissions(self) -> None:
        spec = PluginSpec(category="policy", permissions=["resource.read", "network.egress"])

        assert spec.permissions == [Permission.RESOURCE_READ, Permission.NETWORK_EGRESS]
        assert spec.model_dump(mode="json")["permissions"] == ["resource.read", "network.egress"]

    @pytest.mark.parametrize(
        "name", ["admin", "database", "metrics:read", "device:write", "Resource.Read", ""]
    )
    def test_name_outside_vocabulary_rejected(self, name: str) -> None:
        with pytest.raises(ValidationError, match="permissions"):
            PluginSpec(category="policy", permissions=["resource.read", name])

    def test_duplicate_permissions_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"duplicate permissions: \['resource\.read'\]"):
            PluginSpec(
                category="policy",
                permissions=["resource.read", "decision.evaluate", "resource.read"],
            )


def test_repository_has_plugin_manifests() -> None:
    """防止清单收集为空时下方参数化用例静默通过。"""
    assert len([name for name, _ in _SHIPPED_MANIFESTS if name.startswith("plugins/")]) == 10
    assert any(name.startswith("examples/") for name, _ in _SHIPPED_MANIFESTS)


@pytest.mark.parametrize(
    ("name", "doc"), [pytest.param(name, doc, id=name) for name, doc in _SHIPPED_MANIFESTS]
)
def test_shipped_manifest_permissions_in_vocabulary(name: str, doc: dict[str, Any]) -> None:
    manifest = PluginManifest.model_validate(doc)

    assert [p.value for p in manifest.spec.permissions] == doc["spec"]["permissions"]
