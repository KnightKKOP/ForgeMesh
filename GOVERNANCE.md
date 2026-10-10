# 项目治理（Governance）

本文件说明 ForgeMesh 的治理结构、决策机制与发布政策。目标是在保持**开放协作**的同时，确保架构约束（12 项 ADR 与 4 条不变量）不被随意破坏。

> 上游依据：[`docs/architecture/architecture.md` 第 17 章](docs/architecture/architecture.md)、[`docs/architecture/development-plan.md`](docs/architecture/development-plan.md)。

---

## 1. 治理原则

1. **架构约束优先**：12 项 ADR 与 4 条不变量是硬约束；变更须走 ADR，不得以"实现方便"为由绕过。
2. **开放与透明**：决策、评审与争议在公开渠道进行（安全漏洞除外，见 [`SECURITY.md`](SECURITY.md)）。
3. **共识优先，必要时投票**：优先通过讨论达成共识；无法达成时由维护者按第 3 节裁决。
4. **责任到人**：每个模块有明确 Owner，负责其接口稳定性与代码质量。

---

## 2. 角色与职责

### 2.1 角色定义

| 角色 | 职责 | 产生方式 |
| --- | --- | --- |
| **Contributor（贡献者）** | 提交 Issue/PR、参与讨论 | 任何遵守行为准则的人 |
| **Committer（提交者）** | 审核并合并常规 PR、维护模块 | 由维护者提名、评审后授予 |
| **Module Owner（模块 Owner）** | 负责某模块/子包的接口与技术方向，是相关 PR 的必需评审人 | 由维护者任命 |
| **Maintainer（维护者）** | 项目级决策、发布、治理、ADR 终审 | 现有维护者提名与共识 |
| **Security Team（安全小组）** | 处理漏洞披露与安全修复 | 由维护者指定 |

### 2.2 模块 Owner 一览（随项目演进更新）

| 模块 / 子包 | 责任范围 |
| --- | --- |
| `packages/core-contracts` | 资源基类、Registry/Reconciler/Scheduler Protocol、错误与密钥引用 |
| `packages/resource-model` | 资源模型（InferenceService/Node/分配/租户）、状态机与转移校验 |
| `packages/policy-engine` | 授权、准入、配额与审批规则 |
| `packages/decision-api` | DecisionProvider 与 DecisionRouter（选择/超时/降级/校验） |
| `packages/workflow-api` / `workflow-runtime` | 工作流运行契约与参考执行器 |
| `packages/plugin-sdk` | 插件 Manifest、权限与隔离契约 |
| `packages/control-plane` | 模块化单体控制平面 |
| `packages/node-agent` | 节点侧受控执行组件与 Agent 协议 |
| `packages/cli` | 命令行工具 |
| `gateway` | Go 高性能网关与流式代理 |
| `api/` | 公共 Schema、OpenAPI、Protobuf 契约 |
| `docs/` | 架构文档、ADR、开发指南 |

> 具体 Owner 名单由维护者在 `MAINTAINERS` / 团队目录中维护；本表定义**责任边界**。

---

## 3. 决策机制

### 3.1 日常决策

- 常规代码与文档变更：由 **Committer** 评审合并，涉及某模块的须经该模块 **Owner** 批准。
- 争议先讨论；无法达成共识时，由**维护者**按架构约束与项目目标裁决并记录理由。

### 3.2 ADR 决策流程（重大架构决策）

凡属以下范畴，必须通过 ADR（详见 [`CONTRIBUTING.md` 第 7 节](CONTRIBUTING.md)）：

- 新增或破坏性变更**公共接口 / Schema / 事件 / 插件协议**；
- 变更**数据一致性、安全边界、隔离模型、多租户语义**；
- 引入新的核心依赖或替换关键 Provider。

流程：

```
Proposed ──评审──> Accepted
   │                    │
   └──> Rejected        └──> Superseded by adr-0XX
```

- ADR 需 **≥1 名维护者 + 相关模块 Owner** 评审。
- Accepted 的 ADR 即成为架构约束，实现须遵循；已 Accepted 的结论不得直接改写，应以新 ADR 标记 `Superseded`。
- ADR 与上游架构基线冲突时，**以 architecture.md 为准**；确需变更基线的，走 ADR 修订并同步更新基线文档。

---

## 4. 公共接口兼容性评审

公共契约（API、JSON Schema、OpenAPI、Protobuf、事件、插件协议）是生态基础，按 **ADR-012（所有公共契约版本化）** 管理：

### 4.1 兼容性规则

| 变更类型 | 判定 | 处理 |
| --- | --- | --- |
| 新增可选字段 / 新枚举值（向后兼容） | ✅ 兼容 | minor 版本，须更新 Schema 与契约测试 |
| 删除/重命名字段、改类型、改语义、收紧校验 | ❌ 破坏性 | 须 ADR + 新版本前缀 + 迁移与弃用计划 |
| 新增必需字段 | ❌ 破坏性 | 同上 |

### 4.2 评审要求

- 所有公共契约变更**必须附带契约测试**（`tests/contract`）与 Schema 更新。
- 破坏性变更须**显式在 PR 中标注**，由维护者与受影响模块 Owner 评审通过后方可合并。
- 通过 **Schema、兼容性测试与弃用周期**管理版本演进；禁用"静默破坏"。

---

## 5. 发布与版本政策

### 5.1 版本方案

- API/契约采用**版本前缀**（如 `forgemesh.io/v1alpha1`），与包版本解耦。
- 包/发布版本遵循 **语义化版本（SemVer）**：`MAJOR.MINOR.PATCH`。
  - `MAJOR`：破坏性变更；`MINOR`：向后兼容新增；`PATCH`：向后兼容修复。
- 阶段 0–M1 期间为 `v1alpha1`，接口可能演进；契约冻结后不再破坏性变更（变更须 ADR，见 `development-plan.md` 第 4.4 节）。

### 5.2 发布流程

1. 相关 DoD 全绿、CI 通过。
2. 更新 CHANGELOG 与文档，标注破坏性变更与迁移指引。
3. 制品附带**签名与内容摘要**（见 [`SECURITY.md`](SECURITY.md)）。
4. 由维护者打 tag 并发布；安全相关发布遵循协调披露时间线。

### 5.3 弃用政策

- 任何公共接口的移除**必须经历弃用周期**：
  1. **宣布弃用**（文档 + Release Notes），标注替代方案与移除版本。
  2. **过渡期**：至少跨越一个 minor 版本；运行期发出告警。
  3. **移除**：仅在下一个 `MAJOR`（或 ADR 明确允许时）执行。
- 弃用与移除须在 ADR 或 CHANGELOG 中留痕，保证可追溯。

---

## 6. 冲突与修订

- 对本治理文件的修订须经**维护者共识**，并以 PR 形式公开评审。
- 治理与架构约束的冲突以 [`architecture.md`](docs/architecture/architecture.md) 为准。

---

## 7. 相关文档

- 贡献流程：[`CONTRIBUTING.md`](CONTRIBUTING.md)
- 行为准则：[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- 安全策略：[`SECURITY.md`](SECURITY.md)
- 架构决策记录：[`docs/design-decisions/`](docs/design-decisions/)