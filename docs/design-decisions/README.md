# 架构决策记录（Architecture Decision Records, ADR）

本目录保存 ForgeMesh 的架构决策记录。**ADR-001 ~ ADR-014 已被采纳（Accepted）**，构成架构约束；其中 ADR-001 ~ ADR-012 源自上游架构基线 [`../architecture/architecture.md`](../architecture/architecture.md) 第 2 章与开发计划 [`../architecture/development-plan.md`](../architecture/development-plan.md) 第 1.2 节，ADR-013、ADR-014 为实施期新增决策。

> 参与流程见 [`../../CONTRIBUTING.md`](../../CONTRIBUTING.md) 第 7 节；决策机制见 [`../../GOVERNANCE.md`](../../GOVERNANCE.md) 第 3.2 节。ADR 与上游架构基线冲突时，**以 architecture.md 为准**。

---

## ADR 索引

| ADR | 标题 | 状态 | 架构约束 | 主要落点 |
| --- | --- | --- | --- | --- |
| [ADR-001](adr-001.md) | 微内核与插件分离 | Accepted | 核心定义稳定契约，插件实现具体能力 | WP-0.2、WP-1.5、WP-4.6 |
| [ADR-002](adr-002.md) | 采用声明式资源模型 | Accepted | Desired State 与 Observed State 分离 | 5.1、WP-0.1 |
| [ADR-003](adr-003.md) | WorkEngine 采用可替换 Provider | Accepted | 执行引擎不直接决定领域模型 | 5.4、WP-2.1/2.2 |
| [ADR-004](adr-004.md) | JEV 不是唯一决策系统 | Accepted | 支持规则、JEV、LLM 和其他 Provider | 5.5、WP-2.3/2.4 |
| [ADR-005](adr-005.md) | Scheduler 与 Policy 拥有最终治理权 | Accepted | 决策建议不能直接分配资源或授权 | 5.6、WP-1.4、不变量三测试 |
| [ADR-006](adr-006.md) | 插件按信任级别隔离 | Accepted | 高风险或第三方插件采用隔离执行 | 5.7、WP-0.3、WP-4.6 |
| [ADR-007](adr-007.md) | PostgreSQL 作为权威业务存储 | Accepted | 缓存与事件系统不能取代权威状态 | 第 7 章、WP-1.2 |
| [ADR-008](adr-008.md) | 采用事务性 Outbox 与幂等消费者 | Accepted | 关键业务事件不得依赖非原子双写 | 5.3、WP-1.6 |
| [ADR-009](adr-009.md) | Node Agent 采用受认证的任务协议 | Accepted | 禁止默认执行任意远程 Shell | 5.9、WP-3.2 |
| [ADR-010](adr-010.md) | 采用能力驱动的异构资源模型 | Accepted | 厂商差异通过 Provider 显式表达 | 5.1、WP-5.1 |
| [ADR-011](adr-011.md) | 插件与工作流版本固定 | Accepted | 升级不能破坏正在运行的任务 | WP-2.1、WP-4.6 |
| [ADR-012](adr-012.md) | 所有公共契约版本化 | Accepted | 通过 Schema、兼容性测试与弃用周期管理 | WP-0.5、第 8 章 |
| [ADR-013](adr-013.md) | 权威存储 SQLite 先行、PostgreSQL 为目标态 | Accepted | SQL 方言限定两者兼容子集；PG 独有特性不得进入核心路径 | WP-1.2、ADR-007 |
| [ADR-014](adr-014.md) | 对外 API 采用绑定租户的 API Key 认证 | Accepted | 租户只来自已验证凭证；网关与控制平面各自校验；默认拒绝 | 11.1、14.1、D1.1 |

---

## ADR 模板

新建 ADR 时复制以下模板到 `adr-0XX.md`，保持统一的标题、状态与三节结构（背景 / 决策 / 后果）。

```markdown
# ADR-0XX: <简短、祈使或名词化的决策标题>

- 状态: Proposed | Accepted | Rejected | Superseded by adr-0YY
- 日期: YYYY-MM-DD
- 决策者: <维护者 / 模块 Owner>
- 相关: <关联 ADR、Issue、章节>

## 背景

<描述问题、约束、动机与备选方案。说明为什么现在需要做这个决策。>

## 决策

<明确陈述所采纳的方案。列出必须遵守的约束、边界与责任归属。
如涉及接口，给出关键签名或契约。>

## 后果

<决策带来的正面影响、代价、风险与需后续处理的事项。
说明对兼容性、测试、运维与其它模块的影响。>
```

### 状态说明

| 状态 | 含义 |
| --- | --- |
| `Proposed` | 已提交、待评审 |
| `Accepted` | 已采纳，成为架构约束 |
| `Rejected` | 已否决 |
| `Superseded by adr-0YY` | 被后续 ADR 取代（保留历史，不直接改写结论） |

> 已 `Accepted` 的 ADR 结论不得直接改写；如需变更，应以新 ADR 标记取代关系并说明差异。