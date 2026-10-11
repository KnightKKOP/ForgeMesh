# ForgeMesh 插件开发指南

本指南面向第三方开发者，描述如何使用 ForgeMesh 插件 SDK（`forgemesh-plugin-sdk`）
从零创建、校验、测试、打包并发布一个插件。

---

## 1. 插件架构概述

ForgeMesh 通过插件扩展核心能力，插件以**声明式清单（Manifest）+ Provider 实现**
的形式分发。共有 6 种插件类型：

| kind        | PluginCategory  | 职责                         | Provider 契约方法                       |
| ----------- | --------------- | ---------------------------- | --------------------------------------- |
| `decision`  | `policy`        | 决策引擎（放行/拒绝/降级）   | `evaluate(context) -> dict`             |
| `workflow`  | `capability`    | 工作流运行时                 | `execute(workflow_def, inputs) -> dict` |
| `runtime`   | `runtime`       | 推理运行时（vLLM/TGI 等）    | `start(config)` / `stop(id)` / `health(id)` |
| `gpu`       | `hardware`      | 异构 GPU 发现与分配          | `discover()` / `allocate(request)`      |
| `metering`  | `observability` | 计量与聚合                   | `record(event)` / `aggregate(window)`   |
| `qos`       | `policy`        | 服务质量策略                 | `apply(policy, target)` / `evaluate(metrics)` |

### 生命周期状态机

每个插件实例遵循 6 态治理生命周期：

```
Discovered → Validating → Installed → Enabled ⇄ Disabled → Uninstalled
                         ↘ Uninstalled（校验失败）
```

非法转移抛出 `InvalidStateTransition`。**安装状态与健康状态分离**：
`Installed/Enabled` 描述治理生命周期，不代表运行健康；运行健康由独立健康检查
（`Healthy/Degraded/Unhealthy`）表达。

### 信任分级与隔离

| 信任级别    | 隔离模式         | 说明                         |
| ----------- | ---------------- | ---------------------------- |
| `TRUSTED`   | `IN_PROCESS`     | 进程内执行，允许所有资源     |
| `VERIFIED`  | `RESTRICTED_RPC` | 受限 RPC，禁止直连 DB/FS/Net |
| `UNTRUSTED` | `RESTRICTED_RPC` | 受限 RPC，且禁止声明 `database`/`admin` 权限 |

---

## 2. 快速开始：用 SDK 脚手架创建新插件

### 前置依赖

```bash
pip install forgemesh-plugin-sdk
```

### 生成骨架

```python
from pathlib import Path
from forgemesh.plugin.sdk import PluginSdk

sdk = PluginSdk()
root = sdk.generate_scaffold(
    name="my-decision-plugin",
    kind="decision",
    output_dir=Path("./plugins"),
)
print(root)  # plugins/my-decision-plugin
```

生成的项目结构：

```
plugins/my-decision-plugin/
├── __init__.py
├── provider.py            # Provider 契约实现（含 TODO 占位）
├── manifest.yaml          # 声明式清单
└── tests/
    ├── __init__.py
    └── test_provider.py   # 契约测试骨架
```

> **命名规则**：插件名须匹配 `[a-z][a-z0-9-]*`。目录名保留连字符；
> Python 类名自动转为 PascalCase（`my-decision-plugin` → `MyDecisionPlugin`）。

---

## 3. Manifest 编写指南

`manifest.yaml` 是插件的声明式清单，遵循 `forgemesh.io/v1alpha1` API：

```yaml
apiVersion: forgemesh.io/v1alpha1
kind: PluginManifest
metadata:
  id: my-decision-plugin          # 唯一标识，须与目录名一致
  version: 0.1.0                  # 语义版本
spec:
  category: policy                # 插件类别（见上表）
  compatibility:
    coreApi: v1alpha1             # 兼容的核心 API 版本
  provides:
    - my-decision-plugin          # 提供的能力标识
  requires:
    - forgemesh.runtime.vllm      # 依赖的能力标识（可空）
  permissions:
    - metrics:read                # 最小权限集合
  isolation: restricted-rpc       # 隔离级别
  entrypoint: MyDecisionPlugin    # 入口类名
  artifactDigest: sha256:...      # 制品摘要（打包后自动填入）
  signature: cosign:...           # 制品签名（可选）
  configuration:
    defaultDecision: allow        # 插件配置默认值
  healthCheck:
    mode: rpc
    timeoutMs: 2000
```

### 字段说明

| 字段                         | 必填 | 说明                                       |
| ---------------------------- | ---- | ------------------------------------------ |
| `metadata.id`                | 是   | 插件唯一标识                               |
| `metadata.version`           | 是   | 语义版本（`MAJOR.MINOR.PATCH`）            |
| `spec.category`              | 是   | 插件类别枚举                               |
| `spec.permissions`           | 否   | 最小权限集合；空列表将产生告警             |
| `spec.isolation`             | 否   | 隔离级别；未设置时安装器默认 `restricted-rpc` |
| `spec.entrypoint`            | 否   | 入口类名；未设置将产生告警                 |
| `spec.compatibility.coreApi` | 推荐 | 核心 API 版本锁定，避免不兼容              |

### 校验 Manifest

```python
from forgemesh.plugin.sdk import PluginSdk

sdk = PluginSdk()
result = sdk.validate_manifest("plugins/my-decision-plugin/manifest.yaml")
if not result.valid:
    for e in result.errors:
        print(f"ERROR: {e}")
for w in result.warnings:
    print(f"WARN: {w}")
```

`ValidationResult.valid` 为真当且仅当无 error；warnings 不影响 validity。

---

## 4. Provider 契约实现指南

`provider.py` 导出一个与 `kind` 对应的 Provider 类。每种 kind 的契约方法
与返回值约束如下：

### decision

```python
class MyDecisionPlugin:
    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        """返回至少包含 `decision` 与 `reason` 键的字典。"""
        return {"decision": "allow", "reason": "matched-safe-pattern"}
```

### workflow

```python
class MyWorkflowRuntime:
    def execute(self, workflow_def: dict, inputs: dict) -> dict:
        """返回至少包含 `status` 与 `outputs` 键的字典。"""
        return {"status": "succeeded", "outputs": {"result": 42}}
```

### runtime

```python
class MyRuntimeProvider:
    def start(self, config: dict) -> dict:
        """返回至少包含 `instanceId` 与 `status` 键。"""
    def stop(self, instance_id: str) -> dict:
        """返回包含 `status` 的停止结果。"""
    def health(self, instance_id: str) -> dict:
        """返回包含 `healthy` 布尔键的健康状态。"""
```

### gpu / metering / qos

分别实现 `discover/allocate`、`record/aggregate`、`apply/evaluate`，
详见脚手架生成的 `provider.py` 注释。

> **关键约束**：Provider 方法必须是**同步纯函数式**（无隐式全局状态），
> 返回值必须是可 JSON 序列化的 `dict`/`list`，便于跨 RPC 边界传递。

---

## 5. 测试编写指南

脚手架已在 `tests/test_provider.py` 生成契约测试骨架，使用 `importlib` 按文件
路径加载 `provider.py`，兼容连字符目录名：

```python
import importlib.util
from pathlib import Path
import pytest

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "provider.py"

def _load_provider() -> object:
    spec = importlib.util.spec_from_file_location("provider", _PROVIDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.unit
def test_evaluate_returns_decision_dict() -> None:
    module = _load_provider()
    provider = module.MyDecisionPlugin()
    result = provider.evaluate({"tenant": "team-a"})
    assert isinstance(result, dict)
    assert "decision" in result
```

### 测试标记

| 标记          | 用途                     |
| ------------- | ------------------------ |
| `@pytest.mark.unit`    | 单元测试（每次 PR） |
| `@pytest.mark.contract`| 契约测试（每次 PR） |

### 开放契约测试套件

ForgeMesh 提供开放契约测试套件（`tests/contract/test_plugin_contract_suite.py`），
第三方可直接引用以校验：

- PluginManifest Schema 校验
- Provider 契约一致性（DecisionProvider/WorkflowRuntime/RuntimeProvider）
- 插件生命周期状态机
- 权限模型

---

## 6. 打包与发布流程

### 打包

```python
from forgemesh.plugin.sdk import PluginSdk

sdk = PluginSdk()
artifact = sdk.pack("plugins/my-decision-plugin")
print(artifact.name, artifact.version, artifact.digest, artifact.path)
# my-decision-plugin 0.1.0 sha256:... plugins/my-decision-plugin/my-decision-plugin-0.1.0.tar.gz
```

`pack` 将插件目录（排除 `__pycache__`/`.pytest_cache`）打为 tar.gz，
计算 SHA-256 摘要，并从 `manifest.yaml` 读取名称与版本。

### 签名（可选）

```python
from forgemesh.plugin.signing import PluginSigner, PluginArtifact as SignedArtifact

signer = PluginSigner()
# 用签名模块对摘要做 HMAC-SHA256 签名，详见 forgemesh.plugin.signing
```

### 发布

```python
result = sdk.publish(artifact, registry_url="https://registry.forgemesh.io")
if result.success:
    print(f"published to {result.url} (digest={result.digest})")
```

支持的协议：

| 协议        | 用途                         |
| ----------- | ---------------------------- |
| `memory://` | 本地/测试环境，直接返回成功 |
| `http(s)://`| 真实注册中心上传             |

发布失败返回 `PublishResult(success=False)` 而非抛异常，便于 CI 重试。

---

## 7. 完整示例：从零创建一个 decision 插件

```bash
# 1. 生成骨架
python -c "
from pathlib import Path
from forgemesh.plugin.sdk import PluginSdk
PluginSdk().generate_scaffold('risk-gate', 'decision', Path('./plugins'))
"
```

编辑 `plugins/risk-gate/provider.py`：

```python
from __future__ import annotations
from typing import Any

class RiskGate:
    """基于风险评分的决策门控。"""

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        score = context.get("riskScore", 0)
        if score >= 80:
            return {"decision": "deny", "reason": f"risk score {score} >= 80"}
        return {"decision": "allow", "reason": f"risk score {score} < 80"}
```

编辑 `plugins/risk-gate/manifest.yaml` 的 `spec.permissions` 与 `configuration`：

```yaml
spec:
  permissions:
    - metrics:read
  configuration:
    threshold: 80
```

```bash
# 2. 校验
python -c "
from forgemesh.plugin.sdk import PluginSdk
r = PluginSdk().validate_manifest('plugins/risk-gate/manifest.yaml')
print('valid:', r.valid, 'warnings:', r.warnings)
"

# 3. 测试
pytest plugins/risk-gate/tests/ -v

# 4. 打包 + 发布
python -c "
from forgemesh.plugin.sdk import PluginSdk
sdk = PluginSdk()
art = sdk.pack('plugins/risk-gate')
res = sdk.publish(art, 'memory://registry')
print('published:', res.success, res.url)
"
```

---

## 8. 兼容性与安全认证

发布前建议运行兼容性与安全认证用例（`tests/compat/test_plugin_compatibility.py`）：

- **核心 API 版本兼容**：`PluginVersionCompat.is_compatible(plugin_version, required_version)`
- **升级安全（IVT-8）**：升级不静默改变既有工作流实例的执行定义
- **签名验证**：未签名/摘要不符/签名不符三重拒绝
- **隔离边界**：`UNTRUSTED` 插件不得声明 `database`/`admin` 权限
- **权限校验**：`RESTRICTED_RPC` 模式禁止直连 `database`/`filesystem`/`network`

```python
from forgemesh.plugin.isolation import PluginVersionCompat, TrustLevel, IsolationGuard

compat = PluginVersionCompat()
assert compat.is_compatible("1.2.0", "1.0.0")   # 同 major，且不低于所需版本

guard = IsolationGuard()
guard.validate_manifest(TrustLevel.UNTRUSTED, ["metrics:read"])  # 通过
# guard.validate_manifest(TrustLevel.UNTRUSTED, ["database"])    # 抛 IsolationError
```

---

## 参考

- 插件 SDK 源码：`packages/plugin-sdk/src/forgemesh/plugin/`
- 契约测试套件：`tests/contract/test_plugin_contract_suite.py`
- 兼容性测试：`tests/compat/test_plugin_compatibility.py`
- 相关 ADR：ADR-006（隔离分级）、development-plan 5.7 / 6.3