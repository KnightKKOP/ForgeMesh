# ForgeMesh 插件开发指南

本指南面向第三方开发者，描述如何使用 ForgeMesh 插件 SDK（`forgemesh-plugin-sdk`）
从零创建、校验、测试、打包、签名并发布一个插件。

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

### 运行方式与隔离

插件在宿主进程内运行，ForgeMesh 目前没有为插件建立任何进程边界：插件代码拥有宿主进程的
全部文件系统、网络与内存访问能力。因此清单的 `isolation` 只能声明 `in-process`（未设置同样
按进程内运行）；ADR-006 的受限 RPC 宿主尚未实现，声明 `restricted-rpc` / `sandbox` 的清单
校验失败。

在这个前提下，平台的控制手段是只注册可信的代码，并由管理员决定插件的权限（见第 6 节）：

- **签名**：插件管理器只注册由受信任公钥签名的制品（显式开启的开发模式除外），签名同时
  覆盖制品内容与清单；
- **权限授予**：清单的 `permissions` 只是申请，插件实际拥有的权限是申请与管理员授予的交集。

`forgemesh.plugin.isolation` 中信任级别到隔离模式的映射（`TRUSTED` → `IN_PROCESS`，
`VERIFIED` / `UNTRUSTED` → `RESTRICTED_RPC`）只用于回答「某个信任级别能否使用某项权限」，
不建立进程边界，插件管理器注册插件时也不使用它（见第 8 节）。

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

`manifest.yaml` 是插件的声明式清单，遵循 `forgemesh.io/v1alpha1` API，由
`api/schemas/plugin-manifest.schema.json` 与 `forgemesh.plugin.manifest.PluginManifest` 约束：

```yaml
apiVersion: forgemesh.io/v1alpha1
kind: PluginManifest
metadata:
  id: com.example.my-decision-plugin   # 反向域名风格的唯一标识
  version: 0.1.0                       # 语义版本
spec:
  category: policy                     # 插件类别（见上表）
  compatibility:
    coreApi: v1alpha1                  # 兼容的核心 API 版本
  provides:
    - my-decision-plugin               # 提供的能力标识
  requires: []                         # 依赖的能力标识
  permissions:                         # 申请的权限（见下方权限词表）
    - decision.evaluate
  isolation: in-process                # 目前只能是 in-process
  entrypoint: "provider:MyDecisionPlugin"   # <模块>:<类>
  healthCheck:
    mode: rpc
    timeoutMs: 2000
```

### 字段说明

| 字段                         | 必填 | 说明                                       |
| ---------------------------- | ---- | ------------------------------------------ |
| `metadata.id`                | 是   | 插件唯一标识，反向域名风格（Schema：`^[a-z0-9]+(\.[a-z0-9-]+)+$`，如 `forgemesh.decision.rules`）；脚手架生成的 `my-decision-plugin` 不满足该约束，发布前需要改名。管理员按它授予权限 |
| `metadata.version`           | 是   | 语义版本（`MAJOR.MINOR.PATCH`）            |
| `spec.category`              | 是   | 插件类别枚举                               |
| `spec.permissions`           | 是（Schema） | 申请的权限，取值限于权限词表且不得重复；空列表将产生告警。清单不能给自己授权（见第 6 节） |
| `spec.isolation`             | 否   | 只能是 `in-process`；未设置同样按进程内运行，并产生告警 |
| `spec.entrypoint`            | 否   | 入口点 `<模块>:<类>`（见下文）；未设置将产生告警，仓库内的插件必须声明 |
| `spec.compatibility.coreApi` | 推荐 | 核心 API 版本锁定，避免不兼容              |
| `spec.configuration` / `spec.healthCheck` | 否 | 声明性字段：`configuration` 只允许 `schema`（配置 JSON Schema 路径），`healthCheck` 为 `mode` 与 `timeoutMs`；平台目前不读取这两个字段 |
| `spec.artifactDigest` / `spec.signature` | — | 已弃用，不参与任何校验，计划在 2.0.0 移除（见下方说明） |

标注「是（Schema）」的字段由 JSON Schema 要求（CI 的 `scripts/validate_manifests.py` 用它
校验仓库内的插件），`PluginSdk.validate_manifest` 不强制；`spec.provides` / `spec.requires`
同样由 Schema 要求，可以是空列表。

> **弃用**：`spec.artifactDigest` / `spec.signature` 自 1.1.0 起弃用，计划在 2.0.0 移除。
> 仍写这两个字段的清单照常通过校验，`validate_manifest` 会给出告警；插件管理器注册时既不把它们
> 当作签名，也不拿它们与制品比对。请删除这两个字段，改用
> `python -m forgemesh.plugin.signtool sign` 生成随制品分发的签名文档（见第 6 节「签名」）。

### 权限词表

权限名形如 `<域>.<动作>`。清单的 `spec.permissions`、JSON Schema 的枚举、管理员的权限授予与
`forgemesh.plugin.isolation` 共用 `forgemesh.plugin.permissions.Permission` 这一份词表，词表之外
的名字（如旧写法 `metrics:read`、`database`）校验失败。

| 权限 | 含义 |
| ---- | ---- |
| `resource.read` / `resource.write` | 经宿主 API 读取 / 写入资源对象 |
| `decision.evaluate` | 作为决策 Provider 产出候选 |
| `policy.evaluate` | 参与策略（如 QoS 准入）评估 |
| `workflow.execute` | 执行工作流 |
| `inference.deploy` / `inference.inspect` | 部署与释放推理实例 / 查询推理实例状态 |
| `device.read` / `device.partition` | 读取本机设备信息 / 对设备分区（如 MIG） |
| `metering.read` | 读取计量数据 |
| `event.subscribe` | 订阅平台事件流 |
| `network.egress` | 直接向外部发起网络连接 |
| `filesystem.access` | 直接读写宿主文件系统 |
| `database.access` | 直接连接控制平面数据库 |
| `platform.admin` | 平台管理权限 |

`PluginManager.check_permissions` 供宿主在调用插件前判定权限；插件代码运行在宿主进程内，
它实际能访问什么不受这份清单约束（见第 1 节）。

### 入口点

`spec.entrypoint` 形如 `<模块>:<类>`，模块是相对于插件包的点分路径（如 `provider:RulesProvider`、
`discovery:GPUDiscoverer`）。`forgemesh.plugin.loader.load_entrypoint(plugin_dir, entrypoint)` 把
插件目录作为 Python 包加载（目录必须含 `__init__.py`，目录名可以带连字符），导入其中的模块并
返回入口类；入口类必须定义在插件包内，不能借入口点导入插件目录之外的对象。加载会在当前进程
中执行插件代码。

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
> 返回值必须是可 JSON 序列化的 `dict`/`list`。

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

### 入口冒烟

CI 的 `scripts/plugin_entrypoint_smoke.py` 对 `plugins/` 下的每个插件按 `spec.entrypoint` 加载
入口类：构造器没有必填参数时实例化一次，需要宿主注入依赖（客户端、存储、配置等）时只报告
需要哪些参数。本地运行：`python scripts/plugin_entrypoint_smoke.py`。

---

## 6. 打包、签名与发布流程

### 打包

```python
from forgemesh.plugin.sdk import PluginSdk

sdk = PluginSdk()
artifact = sdk.pack("plugins/my-decision-plugin")
print(artifact.name, artifact.version, artifact.digest, artifact.path)
# com.example.my-decision-plugin 0.1.0 sha256:... plugins/my-decision-plugin/com.example.my-decision-plugin-0.1.0.tar.gz
```

`pack` 将插件目录（排除 `__pycache__`/`.pytest_cache`/`.git`/`node_modules`）打为 tar.gz，
计算 SHA-256 摘要，并从 `manifest.yaml` 读取名称与版本。产物写在插件目录内；目录下已有的
文件（包括上一次的产物与签名文档）都会被打进新的 tar.gz，重新打包前先删除它们。

### 签名

插件管理器默认只注册签过名的制品（见下文「注册」）。签名使用 Ed25519：插件作者持有私钥，
插件管理器只配置公钥，能验签的一方无法伪造签名。签名覆盖制品内容的 SHA-256 摘要与清单摘要
（解析后的清单按键排序、省略默认值的规范 JSON 的 SHA-256，与 YAML 的格式和注释无关），篡改
制品或清单中的任何字段（例如申请的权限）都会使签名失效。签名是随制品分发的独立 JSON 文档，
不写进清单。

```bash
# 1. 生成密钥对：私钥文件权限 0600，公钥写到 author.key.pub，已有文件不会被覆盖；
#    输出 key id 与可追加到插件策略文件 "trustedKeys" 数组的公钥条目
mkdir -p -m 700 ~/.forgemesh
python -m forgemesh.plugin.signtool keygen --private-key ~/.forgemesh/author.key

# 2. 对制品与清单签名，签名文档默认写到 <制品>.sig
python -m forgemesh.plugin.signtool sign --private-key ~/.forgemesh/author.key \
  --manifest plugins/my-decision-plugin/manifest.yaml \
  --artifact plugins/my-decision-plugin/com.example.my-decision-plugin-0.1.0.tar.gz

# 3. 用公钥校验签名，失败时退出码为 1
python -m forgemesh.plugin.signtool verify --public-key ~/.forgemesh/author.key.pub \
  --manifest plugins/my-decision-plugin/manifest.yaml \
  --artifact plugins/my-decision-plugin/com.example.my-decision-plugin-0.1.0.tar.gz
```

签名文档的格式：

```json
{"algorithm": "ed25519", "keyId": "<公钥指纹>", "artifactDigest": "sha256:<hex>",
 "manifestDigest": "sha256:<hex>", "signature": "<base64>"}
```

> 私钥只应由插件作者保管：不要提交到代码仓库，也不要放进插件目录（`pack` 会把目录下的
> 全部文件打进制品）。

### 注册：受信任公钥与权限授予

控制平面侧由插件管理器（`forgemesh.controlplane.plugin_manager.PluginManager`）在注册时校验
签名，并按管理员的授予判定权限。受信任的公钥与权限授予写在管理员维护的插件策略文件（JSON）中：

```json
{
  "trustedKeys": ["-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n"],
  "grants": [
    {"pluginId": "com.example.my-decision-plugin", "permissions": ["decision.evaluate"]}
  ]
}
```

```python
from pathlib import Path

import yaml
from forgemesh.controlplane.plugin_manager import PluginManager
from forgemesh.plugin.manifest import PluginManifest
from forgemesh.plugin.signing import PluginSignature

plugin_dir = Path("plugins/my-decision-plugin")
artifact = plugin_dir / "com.example.my-decision-plugin-0.1.0.tar.gz"
manager = PluginManager.from_policy_file("plugin-policy.json")
plugin_id = manager.register(
    PluginManifest.model_validate(yaml.safe_load((plugin_dir / "manifest.yaml").read_text())),
    artifact=artifact.read_bytes(),
    signature=PluginSignature.model_validate_json(Path(f"{artifact}.sig").read_bytes()),
)
print(plugin_id, manager.check_permissions(plugin_id, {"decision.evaluate"}))
# com.example.my-decision-plugin:0.1.0 True
```

- `register` 先按 `PluginManifest` 重新校验清单，再用 `trustedKeys` 中的公钥校验签名：两个摘要
  都从制品内容与清单重新计算，不信任签名文档中记录的摘要。清单不合法、未签名、签名算法不是
  `ed25519`、签名公钥不受信任、摘要不符、签名不符，或有签名却没有传入制品内容时，抛出
  `PluginRejectedError`，插件不会被注册。
- 未签名的插件只能在显式开启的开发模式下注册：`PluginManager(policy, dev_mode_allow_unsigned=True)`
  或 `PluginManager.from_policy_file(path, dev_mode_allow_unsigned=True)`。开发模式默认关闭，
  只用于本地开发；开启时与每次注册未签名插件时都会记录告警，带签名的插件照常校验。
- 插件实际拥有的权限（`effective_permissions`）是清单申请与 `grants` 授予的交集。`grants` 按
  `metadata.id` 授予，对该插件的所有版本生效；没有授予记录的插件没有任何权限，即使清单申请了
  `platform.admin`。`check_permissions(plugin_id, required)` 按 `required ⊆ 实际权限` 判定，词表
  之外的权限名抛 `ValueError`。
- 策略文件不可读或不合法（未知字段、非法或重复的公钥、词表之外或重复的权限、同一插件多条
  授予、`pluginId` 带版本号）时，`load_plugin_policy` / `from_policy_file` 抛 `PluginPolicyError`。

> 控制平面应用（`forgemesh-control-plane`）目前不构造插件管理器、也不加载插件，因此没有对应的
> 环境变量；插件管理器与策略文件目前由调用方（例如测试）直接使用。

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
| `http(s)://`| 向 `<registry_url>/<name>/<version>` POST 制品元数据（名称、版本、摘要），不上传制品内容与签名文档 |

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

编辑 `plugins/risk-gate/manifest.yaml`：把 `metadata.id` 改成反向域名形式，并在
`spec.permissions` 中申请决策权限：

```yaml
metadata:
  id: com.example.risk-gate
spec:
  permissions:
    - decision.evaluate
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

# 5. 签名（密钥由 signtool keygen 生成，见第 6 节），签名文档随制品分发
python -m forgemesh.plugin.signtool sign --private-key ~/.forgemesh/author.key \
  --manifest plugins/risk-gate/manifest.yaml \
  --artifact plugins/risk-gate/com.example.risk-gate-0.1.0.tar.gz
```

---

## 8. 兼容性与安全认证

发布前建议运行兼容性与安全认证用例（`tests/compat/test_plugin_compatibility.py`）：

- **核心 API 版本兼容**：`PluginVersionCompat.is_compatible(plugin_version, required_version)`
- **升级安全（IVT-8）**：升级不静默改变既有工作流实例的执行定义
- **签名验证**：未签名、制品摘要不符、签名公钥不受信任均被拒绝（算法不符、清单摘要不符与
  签名不符见 `tests/unit/plugins/test_signing.py`，注册路径见
  `tests/unit/controlplane/test_plugin_manager.py`）
- **信任级别与权限**：`IsolationGuard.validate_manifest` 拒绝 `UNTRUSTED` 插件申请
  `database.access` / `platform.admin`；`IsolationGuard.check_access` 拒绝映射到 `RESTRICTED_RPC`
  的信任级别使用 `network.egress` / `filesystem.access` / `database.access`。这两项只是对信任级别
  的检查，不建立进程边界（见第 1 节）

```python
from forgemesh.plugin.isolation import PluginVersionCompat, TrustLevel, IsolationGuard

compat = PluginVersionCompat()
assert compat.is_compatible("1.2.0", "1.0.0")   # 同 major，且不低于所需版本

guard = IsolationGuard()
guard.validate_manifest(TrustLevel.UNTRUSTED, ["resource.read"])      # 通过
# guard.validate_manifest(TrustLevel.UNTRUSTED, ["database.access"])  # 抛 IsolationError
```

---

## 参考

- 插件 SDK 源码：`packages/plugin-sdk/src/forgemesh/plugin/`（权限词表 `permissions.py`、签名
  `signing.py`、签名工具 `signtool.py`、入口加载 `loader.py`）
- 插件管理器：`packages/control-plane/src/forgemesh/controlplane/plugin_manager.py`
- 清单 JSON Schema：`api/schemas/plugin-manifest.schema.json`
- 契约测试套件：`tests/contract/test_plugin_contract_suite.py`
- 兼容性测试：`tests/compat/test_plugin_compatibility.py`
- 入口冒烟：`scripts/plugin_entrypoint_smoke.py`
- 相关 ADR：ADR-006（隔离分级；受限 RPC 宿主尚未实现）、development-plan 5.7 / 6.3