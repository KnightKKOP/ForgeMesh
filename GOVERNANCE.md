# 项目治理（Governance）

本文件说明 ForgeMesh 的治理结构、决策机制与发布政策。目标是在保持**开放协作**的同时，确保架构约束（12 项 ADR 与 4 条不变量）不被随意破坏。

> 上游依据：[`docs/architecture/architecture.md` 第 17 章](docs/architecture/architecture.md)、[`docs/architecture/development-plan.md`](docs/architecture/development-plan.md)。

---

## 0. 愿景与使命

### 0.1 愿景

成为云原生 AI 算力基础设施的开放标准——让任何组织都能以声明式、可治理、多租户的方式编排 GPU 算力、模型服务与 Agent 工作流，而不被单一供应商锁定。

### 0.2 使命

ForgeMesh 致力于：

1. **统一抽象**：以资源模型（InferenceService / Node / Tenant / Allocation）统一描述异构算力与模型服务，屏蔽底层 Provider 差异。
2. **治理优先**：将策略（授权、准入、配额、审批）作为一等公民，确保"决策 ≠ 授权"（不变量三），AI 建议不得绕过策略与调度校验。
3. **开放生态**：通过插件协议与公共契约（Schema / OpenAPI / Protobuf / 事件）构建可扩展生态，核心与扩展解耦。
4. **安全隔离**：多租户边界、密钥不落盘、最小权限、受认证任务协议——安全是设计约束而非事后补丁。
5. **可演进**：通过 ADR 与版本化契约管理架构演进，保证公共接口在 major 版本内稳定，弃用走明确周期。

### 0.3 非目标

- 不做模型训练框架（复用现有生态，如 PyTorch / vLLM / TGI）。
- 不做 GPU 驱动或 Kubernetes 替代品（集成而非重造）。
- 不做全功能 PaaS（聚焦算力编排与治理层）。

---

## 1. 治理模型：BDFL → PMC 演进路线

ForgeMesh 治理随项目成熟度分阶段演进：

| 阶段 | 模型 | 说明 | 当前状态 |
| --- | --- | --- | --- |
| **阶段 0–M1** | **BDFL**（仁慈独裁者） | 项目创始人担任 BDFL，负责架构方向与终审；日常由 Committer 评审 | ✅ 当前 |
| **M2–M3** | **BDFL + 副维护者** | 新增 2–3 名维护者分担模块治理；BDFL 保留架构终审与冲突裁决 | 规划中 |
| **M4+（GA 后）** | **PMC**（项目管理委员会） | 由 5–7 名维护者组成 PMC，投票决策；BDFL 转为架构顾问或荣誉角色 | 规划中 |

### 1.1 BDFL 阶段职责

- 架构方向终审（ADR Accepted / Rejected 的最终裁决）。
- 维护者提名与任免。
- 发布版本号与发布时机的最终决定。
- 冲突无法通过共识解决时的裁决。

### 1.2 PMC 阶段职责（演进目标）

- PMC 成员由社区选举产生，任期 2 年，可连任。
- 重大决策（架构变更、治理修订）需 PMC **≥2/3 多数**投票通过。
- 日常决策仍走 lazy consensus（见第 4 节）。
- PMC 设轮值主席（每季度轮换），负责会议召集与议程整理，无额外决策权。

> 演进触发条件：GA 发布后、活跃维护者 ≥5 名、连续 2 个里程碑按期交付。届时以 ADR 形式正式确立 PMC 章程。

---

## 2. 治理原则

1. **架构约束优先**：12 项 ADR 与 4 条不变量是硬约束；变更须走 ADR，不得以"实现方便"为由绕过。
2. **开放与透明**：决策、评审与争议在公开渠道进行（安全漏洞除外，见 [`SECURITY.md`](SECURITY.md)）。
3. **共识优先，必要时投票**：优先通过讨论达成共识；无法达成时由维护者按第 4 节裁决。
4. **责任到人**：每个模块有明确 Owner，负责其接口稳定性与代码质量。

---

## 3. 角色与职责

### 3.1 角色定义

| 角色 | 职责 | 产生方式 | 核心权限 |
| --- | --- | --- | --- |
| **Contributor（贡献者）** | 提交 Issue/PR、参与讨论 | 任何遵守行为准则的人 | 提交 Issue / PR |
| **Reviewer（评审者）** | 评审指定模块/领域的 PR，给出 approve/request-changes | 由 Committer 或 Maintainer 授权 | PR 评审评论（非合并权） |
| **Committer（提交者）** | 审核并合并常规 PR、维护模块 | 由维护者提名、评审后授予 | 合并非破坏性 PR |
| **Module Owner（模块 Owner）** | 负责某模块/子包的接口与技术方向，是相关 PR 的必需评审人 | 由维护者任命 | 模块内技术决策 + PR 合并 |
| **Maintainer（维护者）** | 项目级决策、发布、治理、ADR 终审 | 现有维护者提名与共识 | 跨模块决策 + 发布 + ADR 终审 |
| **Security Team（安全小组）** | 处理漏洞披露与安全修复 | 由维护者指定 | 私有安全分支 + Advisory 发布 |

### 3.2 角色晋升路径

```
Contributor ──(持续高质量贡献)──> Reviewer ──(展示判断力与领域深度)──> Committer
                                                                              │
                                                              (承担模块责任) ──┤
                                                                              │
                                                                   Module Owner
                                                                              │
                                                              (跨模块贡献+治理) ──> Maintainer
```

**晋升标准（参考性）：**

| 晋升至 | 最低要求 |
| --- | --- |
| Reviewer | 合并 ≥5 个被采纳的 PR，在某领域展现持续参与 |
| Committer | 合并 ≥20 个 PR，含 ≥1 个非平凡特性；获 ≥2 名维护者推荐 |
| Module Owner | 作为 Committer 持续维护某模块 ≥1 个里程碑，获维护者任命 |
| Maintainer | 跨模块贡献 ≥2 个里程碑，展现治理能力与架构判断力；现有维护者共识 |

### 3.3 模块 Owner 一览（随项目演进更新）

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

## 4. 决策机制

### 4.1 Lazy Consensus（默认决策方式）

大多数决策采用 **Lazy Consensus**（惰性共识）：

1. **提议**：在 Issue / Discussion / PR 中明确提出提案与理由。
2. **等待期**：默认等待 **72 小时**（工作日），期间任何人可发表意见。
3. **默认通过**：等待期内无明确反对（-1）即视为共识达成，提案可推进。
4. **有反对则讨论**：若收到 -1，提议者与反对者讨论寻求折中；达成一致后重新进入等待期。

> Lazy Consensus 适用于：常规 PR、文档更新、非破坏性增强、流程改进。

### 4.2 正式投票（争议或重大决策）

当 Lazy Consensus 无法达成共识，或决策属于重大范畴时，启动**正式投票**：

| 决策类型 | 投票主体 | 通过门槛 | 投票期 |
| --- | --- | --- | --- |
| 常规争议 | 维护者 | 简单多数（>50%） | 5 个工作日 |
| ADR（架构决策） | 维护者 + 相关模块 Owner | ≥2 名维护者同意 + 模块 Owner 同意 | 7 个工作日 |
| 破坏性公共接口变更 | 全体维护者 | ≥2/3 多数 | 10 个工作日 |
| 治理文件修订 | 全体维护者 | ≥2/3 多数 | 10 个工作日 |

**投票格式（Apache 风格）：**

```
[Vote] <议题标题>

投票选项：+1（赞成）/ 0（弃权）/ -1（反对，须说明理由）
```

- **+1**：赞成，可附补充意见。
- **0**：弃权，无强烈倾向。
- **-1**：反对，**必须说明理由**并提出替代建议；无理由的 -1 不计入。

### 4.3 日常决策

- 常规代码与文档变更：由 **Committer** 评审合并，涉及某模块的须经该模块 **Owner** 批准。
- 争议先讨论；无法达成共识时，由**维护者**按架构约束与项目目标裁决并记录理由。

### 4.4 ADR 决策流程（重大架构决策）

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

## 5. 公共接口兼容性评审

公共契约（API、JSON Schema、OpenAPI、Protobuf、事件、插件协议）是生态基础，按 **ADR-012（所有公共契约版本化）** 管理：

### 5.1 兼容性规则

| 变更类型 | 判定 | 处理 |
| --- | --- | --- |
| 新增可选字段 / 新枚举值（向后兼容） | ✅ 兼容 | minor 版本，须更新 Schema 与契约测试 |
| 删除/重命名字段、改类型、改语义、收紧校验 | ❌ 破坏性 | 须 ADR + 新版本前缀 + 迁移与弃用计划 |
| 新增必需字段 | ❌ 破坏性 | 同上 |

### 5.2 评审要求

- 所有公共契约变更**必须附带契约测试**（`tests/contract`）与 Schema 更新。
- 破坏性变更须**显式在 PR 中标注**，由维护者与受影响模块 Owner 评审通过后方可合并。
- 通过 **Schema、兼容性测试与弃用周期**管理版本演进；禁用"静默破坏"。

---

## 6. 里程碑与发布周期

### 6.1 里程碑规划

ForgeMesh 采用里程碑驱动的开发节奏，每个里程碑聚焦一组可验证的交付目标：

| 里程碑 | 主题 | 关键交付 | 状态 |
| --- | --- | --- | --- |
| M1 | 核心契约与资源模型 | core-contracts、resource-model、状态机 | 进行中 |
| M2 | 策略与决策 | policy-engine、decision-api、授权与配额 | 规划中 |
| M3 | 控制平面与运行时 | control-plane、workflow-runtime、node-agent | 规划中 |
| M4 | 网关与插件生态 | gateway、plugin-sdk、参考插件 | 规划中 |
| M5 | 可观测与运维 | 审计、计量、告警、混沌验证 | 规划中 |
| M6 | GA 候选 | 文档完善、性能调优、安全审计 | 规划中 |

### 6.2 发布周期

| 类型 | 频率 | 触发条件 | 发布者 |
| --- | --- | --- | --- |
| **预发布（pre-release）** | 按需 | 里程碑阶段性交付 | Committer |
| **minor 发布** | 每个里程碑结束 | DoD 全绿 + CI 通过 + 文档同步 | Maintainer |
| **patch 发布** | 按需 | Bug 修复或安全修复 | Maintainer |
| **major 发布（GA）** | 里程碑 M6 后 | PMC 章程确立 + 安全审计通过 | Maintainer / PMC |

### 6.3 版本方案

- API/契约采用**版本前缀**（如 `forgemesh.io/v1alpha1`），与包版本解耦。
- 包/发布版本遵循 **语义化版本（SemVer）**：`MAJOR.MINOR.PATCH`。
  - `MAJOR`：破坏性变更；`MINOR`：向后兼容新增；`PATCH`：向后兼容修复。
- 阶段 0–M1 期间为 `v1alpha1`，接口可能演进；契约冻结后不再破坏性变更（变更须 ADR，见 `development-plan.md` 第 4.4 节）。

### 6.4 发布流程

1. 相关 DoD 全绿、CI 通过。
2. 更新 CHANGELOG 与文档，标注破坏性变更与迁移指引。
3. 制品附带**签名与内容摘要**（见 [`SECURITY.md`](SECURITY.md)）。
4. 由维护者打 tag 并发布；安全相关发布遵循协调披露时间线。

### 6.5 弃用政策

- 任何公共接口的移除**必须经历弃用周期**（详见 [`docs/governance/deprecation-policy.md`](docs/governance/deprecation-policy.md)）：
  1. **宣布弃用**（文档 + Release Notes），标注替代方案与移除版本。
  2. **过渡期**：至少跨越 2 个 minor 版本；运行期发出告警。
  3. **移除**：仅在下一个 `MAJOR`（或 ADR 明确允许时）执行。
- 弃用与移除须在 ADR 或 CHANGELOG 中留痕，保证可追溯。

---

## 7. 冲突解决流程

冲突在所难免，关键是以透明、可追溯的方式解决。以下流程按升级顺序排列：

### 7.1 第一级：直接讨论

- 当事人在 Issue / PR 评论中直接讨论，寻求技术层面的折中。
- 目标：**48 小时内**达成一致或明确分歧点。

### 7.2 第二级：模块 Owner 介入

- 若 48 小时内未解决，@ 相关 **Module Owner** 介入裁决。
- Module Owner 基于架构约束与模块目标给出裁决意见，并记录理由。
- 目标：**3 个工作日内**给出裁决。

### 7.3 第三级：维护者投票

- 若当事人对 Module Owner 裁决不服，或冲突跨模块，升级至**维护者投票**（见第 4.2 节）。
- 投票结果为最终技术裁决，当事人须尊重结果。

### 7.4 第四级：BDFL / PMC 终裁

- 仅当投票出现平局、或涉及治理/架构根本方向时，由 **BDFL**（当前）或 **PMC 主席**（演进后）做最终裁决。
- 终裁理由须公开记录于相关 Issue / ADR。

### 7.5 行为冲突

- 若冲突涉及人身攻击、骚扰等违反 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) 的行为，**不走技术冲突流程**，直接报告至 <conduct@mekoforge.example>，由社区领导者按行为准则处理。

---

## 8. 相关文档

- 贡献流程：[`CONTRIBUTING.md`](CONTRIBUTING.md)
- 行为准则：[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- 安全策略：[`SECURITY.md`](SECURITY.md)
- 弃用政策：[`docs/governance/deprecation-policy.md`](docs/governance/deprecation-policy.md)
- 架构决策记录：[`docs/design-decisions/`](docs/design-decisions/)