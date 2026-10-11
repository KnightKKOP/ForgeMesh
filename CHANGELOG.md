# Changelog

本文件记录 ForgeMesh 所有显著变更，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

## [1.0.1] - Unreleased

### Fixed

- 勘误：1.0.0 变更日志 P1 段中"PostgreSQL 权威存储（ADR-007）：Repository 模式、迁移脚本"的表述与实际不符——v1.0.0 实际交付为 SQLite 权威存储先行（`SQLiteRegistry` 实现 `ResourceRegistry` 抽象、CAS 乐观并发 `resource_version`），未引入 PostgreSQL 部署与迁移脚本。PostgreSQL 仍为权威存储目标态（ADR-007），过渡决策与迁移判据详见 [ADR-013](docs/design-decisions/adr-013.md)。

### Added

- 新增 [ADR-013](docs/design-decisions/adr-013.md)：权威存储采用 SQLite 先行、PostgreSQL 为目标态的过渡决策。

## [1.0.0] - 2026-10-11

首个稳定版本。API 兼容性冻结，major 版本内保证向后兼容。
完整发布记录见 [docs/releases/v1.0.md](docs/releases/v1.0.md)。

### Added

#### P0 — 工程骨架与领域契约（125 测试）
- 初始化 monorepo 工程骨架（Python 3.12 + Go 1.22 + ruff + pytest）
- 建立领域契约包 `core-contracts`：实体、值对象、领域事件不变量
- 建立资源模型包 `resource-model`：声明式资源（apiVersion/kind/metadata/spec/status）
- 定义 OpenAPI 3.1 规范骨架（`api/openapi/openapi.yaml`）
- 定义 JSON Schema 权威契约（`api/schemas/`：inference-service、device-profile、event、plugin-manifest）
- 引入 Go 网关骨架（`gateway/`）：/api/v1/ 版本前缀、统一错误信封
- 搭建 CI 流水线（`.github/workflows/ci.yml`）：Python lint + test、Go build + test
- 编写架构基线文档 `docs/architecture/architecture.md`（11.7 万字）
- 编写开发计划文档 `docs/architecture/development-plan.md`（13.9 万字）
- 采纳 ADR-001 ~ ADR-012 架构决策记录
- 制定 CONTRIBUTING、GOVERNANCE、SECURITY、CODE_OF_CONDUCT 治理文档
- 共 140 文件、125 测试

#### P1 — 核心控制平面（215 测试，累计）
- 控制平面包 `control-plane`：Registry（资源注册表）、Controller（调和循环）
- 策略引擎包 `policy-engine`：Scheduler/Policy 最终治理权（ADR-005 不变量）
- 插件管理器 PluginManager：发现、加载、生命周期管理
- 审计日志 Audit：所有治理操作可查询、可追溯
- 事务性 Outbox（ADR-008）：事件与状态原子写入、幂等消费者
- 对外 REST API 实现（FastAPI）：推理服务 CRUD、工作流启动、决策请求
- CLI 工具包 `cli`：声明式资源管理命令行
- SQLite 权威存储先行（ADR-013 过渡决策）：ResourceRegistry 抽象、CAS 乐观并发（resource_version）；PostgreSQL 迁移判据见 ADR-013
- 统一响应信封与错误码体系
- 累计 215 测试

#### P2 — 工作流与决策（341 测试，累计）
- 工作流运行时包 `workflow-runtime`：WorkEngine 可替换 Provider 架构（ADR-003）
- 参考执行器插件 `workflow-reference`：顺序/并行/补偿编排
- 工作流 API 包 `workflow-api`：定义版本化、运行状态机
- 决策 API 包 `decision-api`：结构化决策请求与追踪
- JEV 决策插件 `decision-jev`：JSON 表达式求值
- 规则决策插件 `decision-rules`：规则引擎 Provider
- 熔断器 CircuitBreaker：Provider 故障隔离与恢复
- 降级路径 Degradation：决策降级标记与可观测追踪
- 决策插件接口标准化（ADR-004：JEV 非唯一决策系统）
- 累计 341 测试

#### P3 — 执行平面与 GPU 生命周期（503 测试，累计）
- Node Agent 包 `node-agent`：受认证任务协议（ADR-009，禁止默认远程 Shell）
- gRPC 通信层：控制平面 ↔ Node Agent 双向流
- GPU NVIDIA 插件 `gpu-nvidia`：GPU 发现、拓扑、显存管理
- vLLM 运行时插件 `runtime-vllm`：模型加载、推理服务生命周期
- 执行平面 E2E 测试：控制平面 → Node Agent → GPU → 推理全链路
- 混沌测试 Chaos：网络分区、节点宕机、GPU 故障注入
- 能力驱动异构资源模型（ADR-010）：厂商差异通过 Provider 显式表达
- 累计 503 测试

#### P4 — 生产级治理（712 测试，累计）
- 配额管理 `qos-basic` 插件：租户级资源配额与限流
- QoS 调度优先级：高/中/低优先级队列与抢占
- 计量 `metering-basic` 插件：GPU 时、推理请求数、令牌数计量
- 可观测性 Observability：metrics（Prometheus）、tracing（OpenTelemetry）、structured logging
- 诊断 Diagnostics：健康检查、就绪探针、运行时自检
- 插件安全沙箱（ADR-006）：按信任级别隔离执行，高风险插件受限
- 安全测试：认证/授权/注入/越权/SSRF 覆盖
- 插件与工作流版本固定（ADR-011）：升级不破坏运行中任务
- 累计 712 测试

#### P5 — 扩展与生态（898 测试，累计）
- 异构资源 MIG（Multi-Instance GPU）支持：GPU 分区与共享
- Temporal 工作流插件 `workflow-temporal`：生产级工作流编排引擎集成
- TGI 运行时插件 `runtime-tgi`：HuggingFace TGI 推理后端集成
- 插件 SDK 包 `plugin-sdk`：脚手架生成、开发工具链、契约测试套件
- 治理文档 `docs/governance/`：弃用政策（deprecation-policy.md）
- 插件开发指南 `docs/plugin-development/guide.md`
- 运维文档 `docs/operations/`
- 兼容性测试套件 `tests/compat/`：API 契约回归保护
- 插件契约测试套件 `tests/contract/`
- OpenAPI v1.0 稳定标记（`x-stability: stable`）与弃用扩展字段（`x-deprecated`）
- 架构文档纳入 CI 检查
- 累计 898 测试

### Changed

- OpenAPI `info.version` 从 `0.0.1` 升级至 `1.0.0`
- OpenAPI 添加 `x-stability: stable` 扩展字段标记稳定 API
- OpenAPI 添加 `x-deprecation-policy` 扩展字段定义弃用标记规则
- OpenAPI tags 添加描述信息
- CI 流水线新增架构文档检查 job（docs/architecture、docs/design-decisions、api/openapi、api/schemas）
- 所有端点从 501 Not Implemented 占位升级为正式实现
- ruff target-version 固定为 py312
- README 更新为 v1.0 稳定版描述

### Fixed

- 修复插件加载时信任级别校验遗漏问题
- 修复 Outbox 消费者在重复消息时的幂等性问题
- 修复 Node Agent 在 GPU 热拔插时的资源泄漏
- 修复工作流补偿在并发场景下的状态竞争
- 修复决策追踪在降级路径下的上下文丢失

### Security

- 插件安全沙箱按信任级别隔离执行（ADR-006）
- Node Agent 采用受认证任务协议，禁止默认执行任意远程 Shell（ADR-009）
- 所有 API 端点强制认证与租户隔离
- SSRF 防护：外部 URL 请求经白名单校验
- 插件 manifest 签名验证

### Deprecated

- 无。v1.0 为首个稳定版本，无弃用项。
- 弃用政策：未来版本中弃用的端点或字段将使用 `x-deprecated: true` 标记，
  至少保留 2 个 minor 版本后移除。详见 [docs/governance/deprecation-policy.md](docs/governance/deprecation-policy.md)。

---

## 版本对比链接

[1.0.0]: https://github.com/forgemesh/forgemesh/releases/tag/v1.0.0