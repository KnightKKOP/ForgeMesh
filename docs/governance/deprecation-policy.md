# 弃用政策（Deprecation Policy）

本文件定义 ForgeMesh 公共接口（API、Schema、OpenAPI、Protobuf、事件、插件协议、CLI 命令）的弃用与移除流程。目标是保证生态使用者在版本演进中有充足的迁移时间与明确的迁移路径。

> 上游依据：[`GOVERNANCE.md` 第 5–6 节](../../GOVERNANCE.md)、[ADR-012（所有公共契约版本化）](../design-decisions/)。

---

## 1. 兼容性保证

### 1.1 SemVer 承诺

ForgeMesh 遵循[语义化版本（SemVer）](https://semver.org/lang/zh-CN/)：

| 版本号变更 | 含义 | 兼容性保证 |
| --- | --- | --- |
| `MAJOR`（x.0.0） | 破坏性变更 | **不保证**向后兼容；须提供迁移指南 |
| `MINOR`（1.x.0） | 向后兼容新增 | ✅ 保证向后兼容；可包含弃用声明（但非移除） |
| `PATCH`（1.0.x） | 向后兼容修复 | ✅ 保证向后兼容 |

### 1.2 Major 版本内 API 稳定

- 在同一 `MAJOR` 版本内，所有已发布的公共接口保持**向后兼容**。本节承诺自 API 标注为 `stable` 起生效；`beta` 阶段的规则见 1.4 节。
- 破坏性变更（删除字段、改类型、改语义、收紧校验、新增必需字段）**仅在 `MAJOR` 版本升级时**允许。
- `v1alpha1` / `v1beta1` 等 API 版本前缀在**对应阶段内**遵循同样规则；阶段升级（alpha → beta → GA）视为 MAJOR 级变更，须走完整弃用流程。

### 1.3 不在弃用政策范围内的内容

以下变更不受弃用政策保护（但仍应在 Release Notes 中说明）：

- **内部/私有 API**：以下划线前缀（`_`）或明确标注 `@internal` 的接口。
- **实验性功能**：标注 `@experimental` 或 `EXPERIMENTAL` 的接口，可在任何版本变更。
- **配置默认值**：默认配置值调整（但须在 Release Notes 中说明）。
- **错误消息文本**：错误消息的措辞调整（错误类型与错误码保持稳定）。

### 1.4 预览阶段（beta）

API 的稳定性以 OpenAPI 的 `info.x-stability` 为准，当前为 `beta`。v1.0.0 发布时标注为 `stable`，这一标注与实现不符，已经更正（见 [`CHANGELOG.md`](../../CHANGELOG.md) 1.1.0）。

- `beta` 阶段的 `MINOR` 版本可以包含破坏性变更，但必须在 `CHANGELOG.md` 中以「**破坏性**」标注，并写明影响范围与迁移方式。
- 弃用仍按第 2 节声明，并在声明中写明计划移除的版本；警告期可以短于第 3.2 节的要求。
- `x-stability` 改为 `stable` 之前，应当满足 [架构验收标准](../architecture/architecture.md)（第 20 节）；改为 `stable` 之后，适用 1.1 至 1.3 节的全部承诺。

---

## 2. 弃用声明流程

### 2.1 何时声明弃用

当一个公共接口不再推荐使用、且计划在未来移除时，须正式声明弃用。声明须在**移除前至少 2 个 minor 版本**完成。

### 2.2 声明步骤

1. **提交 PR / ADR**：
   - 在代码中为被弃用接口添加弃用标注（见 2.3 节）。
   - 若涉及架构决策，提交 ADR 说明弃用理由与替代方案。
2. **更新文档**：
   - 在接口 docstring 中添加 `.. deprecated::` 标注，说明替代方案与计划移除版本。
   - 在 `CHANGELOG.md` 中以 `[deprecated]` 标签记录。
   - 在相关文档页面添加弃用横幅。
3. **运行期告警**：
   - 被弃用接口在被调用时发出 `DeprecationWarning`（Python）或日志告警（Go / CLI）。
   - 告警消息包含：替代方案、文档链接、计划移除版本。
4. **Release Notes**：
   - 在发布说明中列出所有新增弃用声明，醒目标注迁移指引。

### 2.3 代码标注规范

**Python（公共函数 / 类 / 方法）：**

```python
import warnings
from typing import Any

def old_api(param: Any) -> Any:
    """[已弃用] 请改用 new_api。

    .. deprecated:: 1.3.0
       请改用 :func:`new_api`，将在 2.0.0 移除。
    """
    warnings.warn(
        "old_api 已弃用，请改用 new_api。将在 2.0.0 移除。详见 docs/governance/deprecation-policy.md",
        DeprecationWarning,
        stacklevel=2,
    )
    return new_api(param)
```

**Go（公共函数）：**

```go
// Deprecated: 请改用 NewAPI，将在 v2.0.0 移除。
func OldAPI(param string) string {
    log.Warn("OldAPI 已弃用，请改用 NewAPI，将在 v2.0.0 移除")
    return NewAPI(param)
}
```

**CLI 命令：**

```
$ forgemesh old-command
⚠ 警告: `old-command` 已弃用，请改用 `new-command`。将在 v2.0.0 移除。
详见: docs/governance/deprecation-policy.md
```

---

## 3. 弃用时间线

### 3.1 标准时间线

```
声明弃用          2 个 minor 版本警告期          移除
  │                    │                        │
  ▼                    ▼                        ▼
v1.3.0 ──────────── v1.4.0 ── v1.5.0 ──────── v2.0.0
  │                    │           │              │
  │                    │           │              │
  └── 添加标注 +       └── 持续    └── 最后警告   └── 移除接口
      DeprecationWarning      告警          告警        更新文档
```

| 阶段 | 版本 | 行动 |
| --- | --- | --- |
| **声明** | `v1.3.0`（minor） | 添加弃用标注 + `DeprecationWarning` + 文档更新 + CHANGELOG |
| **警告期 1** | `v1.4.0`（minor） | 维持告警；Release Notes 再次提醒 |
| **警告期 2** | `v1.5.0`（minor） | 维持告警；Release Notes 标注"下个 major 将移除" |
| **移除** | `v2.0.0`（major） | 移除接口 + 更新文档 + 迁移指南 + 契约测试更新 |

### 3.2 最短弃用期

- **标准接口**：至少 **2 个 minor 版本**的警告期。
- **核心契约**（资源模型、策略引擎、插件协议）：至少 **3 个 minor 版本**的警告期，给予生态更长迁移时间。
- **安全相关接口**：若弃用原因涉及安全（如不安全的默认行为），可经 ADR 批准**缩短**警告期，但须在 Release Notes 中醒目说明。

### 3.3 加速弃用

在以下情况下，经维护者投票（≥2/3 多数）可加速弃用：

- 接口存在已知安全漏洞且无法在不破坏兼容性的前提下修复。
- 接口依赖的第三方库已停止维护且存在安全风险。
- 接口从未有已知使用者（经社区调研确认）。

加速弃用须在 ADR 中记录理由与调研结论。

---

## 4. 迁移指南要求

每次弃用声明和移除都须附带**迁移指南**，确保使用者能顺畅过渡。

### 4.1 迁移指南内容清单

迁移指南须包含以下内容：

- [ ] **被弃用接口**：完整名称与签名。
- [ ] **替代接口**：新接口的完整名称与签名（若无直接替代，说明原因与变通方案）。
- [ ] **行为差异**：新旧接口在语义、参数、返回值、错误处理上的差异。
- [ ] **代码示例**：迁移前后的代码对比（before / after）。
- [ ] **配置迁移**：若涉及配置项变更，提供配置映射表。
- [ ] **兼容性说明**：新接口的最低版本要求、向后兼容性。
- [ ] **常见问题**：迁移过程中可能遇到的问题与解决方案。

### 4.2 迁移指南位置

| 类型 | 位置 |
| --- | --- |
| 单个接口弃用 | 接口 docstring 的 `.. deprecated::` 标注 + `CHANGELOG.md` |
| 批量 / 重大弃用 | `docs/migrations/vX-to-vY.md` 专门迁移文档 |
| CLI 命令弃用 | CLI 帮助文本 + `docs/cli/migration.md` |
| Schema / 事件弃用 | `api/` 目录下的迁移说明 + 契约测试中的迁移用例 |

### 4.3 迁移指南示例

```markdown
## 从 `old_api` 迁移到 `new_api`

### 被弃用接口

```python
def old_api(resource: dict) -> dict: ...
```

### 替代接口

```python
def new_api(resource: InferenceService) -> InferenceService: ...
```

### 行为差异

| 方面 | `old_api` | `new_api` |
| --- | --- | --- |
| 参数类型 | `dict`（未校验） | `InferenceService`（Pydantic 模型，强校验） |
| 返回值 | `dict` | `InferenceService` |
| 校验 | 调用方自行校验 | 框架自动校验，校验失败抛 `ValidationError` |

### 代码示例

**迁移前：**

```python
result = old_api({"name": "my-service", "replicas": 2})
```

**迁移后：**

```python
result = new_api(InferenceService(name="my-service", replicas=2))
```
```

---

## 5. 弃用跟踪

### 5.1 弃用登记表

所有活跃的弃用声明记录在 `docs/governance/deprecation-registry.md`（或 CHANGELOG 的弃用段落）中：

| 接口 | 弃用版本 | 计划移除版本 | 替代方案 | ADR |
| --- | --- | --- | --- | --- |
| `old_api()` | v1.3.0 | v2.0.0 | `new_api()` | ADR-0XX |
| `--old-flag` | v1.4.0 | v2.0.0 | `--new-flag` | — |

### 5.2 移除前检查

在 major 版本中移除已弃用接口前，须确认：

1. 弃用期已满足最短要求（见 3.2 节）。
2. 迁移指南已完备（见 4.1 节）。
3. 契约测试已更新（移除旧接口测试，保留迁移测试）。
4. Release Notes 已醒目标注移除项。
5. 无已知重要使用者仍在依赖（经社区调研）。

---

## 6. 相关文档

- 项目治理：[`GOVERNANCE.md`](../../GOVERNANCE.md)
- 贡献指南：[`CONTRIBUTING.md`](../../CONTRIBUTING.md)
- 架构决策记录：[`docs/design-decisions/`](../design-decisions/)
- 变更日志：[`CHANGELOG.md`](../../CHANGELOG.md)