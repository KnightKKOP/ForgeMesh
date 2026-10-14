# ForgeMesh 模块-代码真实映射（Module Mapping）

> **版本：** 1.1.0 · **日期：** 2026-10-11 · **基线：** `feat/v1.1-trust-baseline`（源自 main = `647a8c7`）
> **依据：** 《ForgeMesh 架构优化与开发指导 v1.1》第 11 / 17.3 章 + 附录 A；[architecture.md](architecture.md)；[v1.1-development-plan.md](v1.1-development-plan.md) WP-B1
> **目的：** 把逻辑架构模块与仓库真实代码连接起来——每一条 `当前代码路径` 与 `测试路径` 均经 `ls` / `glob` 实地核对存在；每一条依赖边界结论均来自实际 `grep` import 扫描（证据见第 3 节）。**不把"README 列出"当作"已验证"。**

---

## 0. 使用说明与状态口径

### 0.1 状态口径（四档，从严判定）

| 状态 | 判定标准 |
|---|---|
| **已实现且有测试覆盖** | 源码存在 **且** 存在对应测试文件（glob 核对），本文撰写时本地 `pytest tests/ -q` 全绿（993 passed，2026-10-11） |
| **实现待核验** | 源码存在但无直接测试覆盖，或关键行为尚未被自动化验证 |
| **缺失** | 指导文档/计划提及但仓库中不存在对应工件 |
| **计划中** | 目标态工件（预置占位、声明"暂不实际运行"或排期于 v1.1 计划） |

### 0.2 循证方法

- 路径存在性：`ls` / `find` / glob 实测（本文所有路径均已在 2026-10-11 于本分支核对）。
- 依赖边界：`grep -rn` 对 `--include="*.py"` 的 import 扫描，命令与输出结论逐条记录于第 3 节。
- 测试基线：`python -m pytest tests/ -q` → **993 passed**（本机 2026-10-11，5.34s）。
- Go 侧：本机无 Go 工具链，`go test` 以 CI `go` job 为准（gateway 31 个 `*_test.go` 经 glob 核对存在）。

---

## 1. 模块速查总览

| # | 架构模块 | 当前代码路径（主） | 运行形态 | 状态 |
|---|---|---|---|---|
| 1 | 外部 API 契约 | `api/openapi/`、`api/schemas/`、`api/protobuf/` | 静态契约工件 | 已实现且有测试覆盖 |
| 2 | Go 接入/推理网关 | `gateway/` | 独立进程（Go 二进制） | 已实现且有测试覆盖 |
| 3 | 核心契约 | `packages/core-contracts/` | 进程内库 | 已实现且有测试覆盖 |
| 4 | 资源模型 | `packages/resource-model/` | 进程内库 | 已实现且有测试覆盖 |
| 5 | 控制平面实现 | `packages/control-plane/` | 单实例控制平面进程 | 已实现且有测试覆盖 |
| 6 | Policy | `packages/policy-engine/` | 进程内模块 | 已实现且有测试覆盖 |
| 7 | Decision API | `packages/decision-api/` | 进程内模块 | 已实现且有测试覆盖 |
| 8 | Decision Providers | `plugins/decision-rules`、`decision-jev`、`decision-typesafe-jev` | 插件 | 已实现且有测试覆盖 |
| 9 | Workflow API | `packages/workflow-api/` | 进程内库 | 已实现且有测试覆盖 |
| 10 | Workflow Runtime | `packages/workflow-runtime/` | 进程内模块 | 已实现且有测试覆盖 |
| 11 | Workflow Providers | `plugins/workflow-reference`、`workflow-temporal` | 插件 | 已实现且有测试覆盖 |
| 12 | Node Agent | `packages/node-agent/` + `api/protobuf/agent.proto` | 独立进程（节点侧） | 已实现且有测试覆盖 |
| 13 | Hardware Provider | `plugins/gpu-nvidia/` | 插件 | 已实现且有测试覆盖 |
| 14 | Runtime Providers | `plugins/runtime-vllm`、`runtime-tgi` | 插件 | 已实现且有测试覆盖 |
| 15 | QoS / Metering | `plugins/qos-basic`、`metering-basic` | 插件 | 已实现且有测试覆盖 |
| 16 | 持久化 | `control-plane/registry/store.py` 等（SQLite-first） | 进程内（SQLite 文件） | 已实现且有测试覆盖 |
| 17 | 开发与 CI | `Makefile`、根 `pyproject.toml`、`.github/workflows/ci.yml` | 开发工具链 | 已实现（细节见小节） |
| 18 | 部署与运维 | `deploy/`、`docs/operations/` | 部署工件 + 运维文档 | compose 已实现且有测试覆盖；k8s 清单实现待核验（见 2.18） |
| 19 | 命令行工具 | `packages/cli/` | 命令行进程（REST API 客户端） | 已实现且有测试覆盖 |

> 插件共 **10 个**（含 `decision-typesafe-jev`，PR #3 已合入 main），全部在 `plugins/` 平铺目录，每个目录都是 Python 包（`__init__.py` + `manifest.yaml` + 实现模块），由 `api/schemas/plugin-manifest.schema.json` 约束。清单 `spec.entrypoint`（`<模块>:<类>`，如 `provider:RulesProvider`、`deploy:VLLMDeployer`）由 `forgemesh.plugin.loader` 在当前进程内加载；插件都在宿主进程内运行，没有进程隔离（`isolation` 只能声明 `in-process`，见偏差 D3）。

---

## 2. 模块明细

### 2.1 外部 API 契约

| 字段 | 内容 |
|---|---|
| 架构模块 | 外部 API 契约（OpenAPI / JSON Schema / Protobuf） |
| 当前代码路径 | `api/openapi/openapi.yaml`；`api/schemas/inference-service.schema.json`、`device-profile.schema.json`、`event.schema.json`、`plugin-manifest.schema.json`；`api/protobuf/agent.proto` |
| 核心契约 | OpenAPI **3.1.0**（`ForgeMesh Control Plane API` version **1.1.0**，`x-stability: beta`，`/api/v1/` 前缀经 Go 网关）；4 份 JSON Schema（资源权威 Schema）；Protobuf **proto3** `forgemesh.agent.v1alpha1`（草案：ADR-009 无 shell 任务、`idempotency_key`、`correlation_id`） |
| 运行形态 | 静态契约工件：被 CI `architecture-docs` job 校验、被控制平面/网关/Agent 实现引用 |
| 数据所有权 | 无（仅定义协议，不持有数据） |
| 允许依赖 | 无（纯声明文件，可引用彼此的语义） |
| 禁止依赖 | 任何实现代码、任何第三方运行时 |
| 测试路径 | `tests/contract/test_schema_validation.py`、`tests/contract/test_cross_representation.py`、`tests/contract/test_openapi_contract.py`（控制平面路由与 `openapi.yaml` 双向核对，逐一调用每个操作，响应按契约中的 schema 校验） |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；WP-A2.3 将加强 OpenAPI 语义校验（operationId 唯一、引用完整性） |

### 2.2 Go 接入/推理网关

| 字段 | 内容 |
|---|---|
| 架构模块 | Data Plane Gateway（L1 推理流量入口，architecture.md 3.1 L1 / Dev-5） |
| 当前代码路径 | `gateway/cmd/gateway/main.go`；`gateway/internal/{config,health,metrics,middleware,router,stream,upstream}/`（共 53 个 `.go` 文件，其中 31 个 `_test.go`，glob 核对）；`gateway/go.mod`（`github.com/Mekoforge/ForgeMesh/gateway`，go 1.22）；`gateway/Makefile`、`gateway/README.md` |
| 核心契约 | `/api/v1/` REST + SSE 流式转发（见 openapi.yaml）；中间件链 `Observe → Recovery → Correlation → TraceContext → AccessLog → Authenticate → RateLimit → LimitRequestBody → Route`；API key 认证（ADR-014，`middleware/{authz,apikey}.go`）；上游解析 `upstream/resolver.go`（按命名空间解析 ready 实例、`protocol` → 实例推理路径、合并回源、健康摘流）；实例地址限制 `stream/addrpolicy.go`；`/metrics`（Prometheus 文本格式，不经认证/限流，`metrics/{metrics,gateway}.go`，测试用解析 `metrics/metricstest`）；W3C `traceparent` 沿用/生成并转发、访问日志（`middleware/{observe,tracecontext,accesslog}.go`） |
| 运行形态 | 独立进程（Go 二进制），经 `GATEWAY_UPSTREAM_URL` 转发至控制平面 |
| 数据所有权 | 无权威数据（运行态转发；端点解析缓存为派生数据） |
| 允许依赖 | 控制平面 HTTP API（上游）、Go 标准库 |
| 禁止依赖 | Python 进程内模块、直连 SQLite/数据库、任何插件、LLM SDK |
| 测试路径 | `gateway/internal/` 下 31 个 `*_test.go`：`config_test.go`、`health_test.go`、`metrics/{gateway,metrics}_test.go`、`metrics/metricstest/metricstest_test.go`、`middleware/{accesslog,apikey,authz,bodylimit,chain,correlation,observe,ratelimit,recovery,tracecontext}_test.go`、`router/{auth,errors,metrics,resolver,router,tenant_security,tracecontext}_test.go`、`stream/{addrpolicy,metrics,proxy,sse_cancel,tracecontext}_test.go`、`upstream/{client,metrics,resolver,tracecontext}_test.go`；CI 以 `-race` 运行 |
| 状态 | 已实现且有测试覆盖（本机无 Go 工具链，`go test` 以 CI `go` job 为准；`find gateway -name "*.py"` = 0，纯 Go 已循证，见 3.4） |
| 演进动作 | 保留；WP-D1 补多租户安全验证（tenant 绑定、Resolver 行为、SSE 取消、链序） |

### 2.3 核心契约（core-contracts）

| 字段 | 内容 |
|---|---|
| 架构模块 | 微内核契约（architecture.md 第 4 章） |
| 当前代码路径 | `packages/core-contracts/src/forgemesh/core/{objects,errors,registry,reconciler,scheduler,secrets}.py`（发行名 `forgemesh-core`，PEP 420 命名空间包） |
| 核心契约 | `ObjectMeta` / `Condition` / `Resource`（objects.py）；`ResourceRegistry(ABC)`（registry.py）；`Reconciler(ABC)` + `ReconcileResult`（reconciler.py）；`Scheduler(ABC)` + `ScheduleRequest`（`ResourceRef` 归属身份 + 幂等键）/ `FeasibilityResult` / `AllocationRef`（含 `AllocatedDevice`）（scheduler.py）；errors 异常体系；secrets 接口 |
| 运行形态 | 进程内库（被控制平面、runtime、Agent 侧共同依赖的稳定抽象层） |
| 数据所有权 | 无（仅定义接口与值对象） |
| 允许依赖 | `pydantic`、标准库 |
| 禁止依赖 | 插件、DB 驱动、Web 框架、LLM SDK（扫描一验证 ✅，见 3.1） |
| 测试路径 | `tests/unit/core/test_objects.py`、`tests/unit/core/test_scheduler_contract.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.4 资源模型（resource-model）

| 字段 | 内容 |
|---|---|
| 架构模块 | 资源对象与状态机（architecture.md 5.1–5.3） |
| 当前代码路径 | `packages/resource-model/src/forgemesh/resource/{node,tenant,allocation,inference,states}.py` |
| 核心契约 | `Node` / `Tenant` / `Allocation` / `InferenceService` pydantic 模型；资源状态机（states.py，含合法迁移校验） |
| 运行形态 | 进程内库 |
| 数据所有权 | 无（定义资源形状；权威实例由控制平面 Registry 持有） |
| 允许依赖 | `forgemesh.core`（core-contracts）、pydantic |
| 禁止依赖 | 插件、DB 驱动、Web 框架、控制平面内部实现 |
| 测试路径 | `tests/unit/resource/test_states.py`、`tests/unit/resource/test_inference.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.5 控制平面实现（control-plane）

| 字段 | 内容 |
|---|---|
| 架构模块 | L2 Core Control Plane（权威状态、协调、插件管理、审计） |
| 当前代码路径 | `packages/control-plane/src/forgemesh/controlplane/`：`registry/{models,store}.py`、`reconciler/{core,dedup}.py`、`controller/{manager,queue}.py`、`scheduler/{inventory,first_fit}.py`、`deployment/{launcher,workflow,release,recovery,config,network,process,process_records,fake,vllm_process,tgi_process,memory}.py`、`api/{routes,dependencies,errors,updates,risk,middleware}.py`、`plugin_manager.py`、`outbox.py`、`events.py`、`publishers.py`、`audit.py`、`pagetokens.py`、`tasks.py`、`heartbeat.py`、`observability.py`、`metrics.py`、`telemetry.py`、`tracecontext.py`、`logs.py`、`diagnostics.py`、`app.py`、`version.py`、`auth.py`、`keygen.py`、`sqlite.py`（共 52 个 `.py`；其中 `auth.py` / `keygen.py` / `api/errors.py` 为 API 认证与错误信封，见 ADR-014；`api/updates.py` 为 `PUT` / `PATCH` 的更新规则（JSON Merge Patch、ETag / `If-Match`），`api/risk.py` 按操作推导决策请求的风险等级；`metrics.py` 为不依赖第三方库的 Prometheus 文本格式指标，`telemetry.py` 定义 `GET /metrics` 输出的指标，`tracecontext.py` 解析 W3C `traceparent`，`api/middleware.py` 为每个请求绑定关联 ID 与 trace-id 并记录请求指标，`logs.py` 为带这两个 ID 的 JSON / 文本日志；`observability.py` 的内存记录器不经 `/metrics` 输出；`controller/` 驱动协调器并回写 status；`scheduler/` 按静态加速器清单首次适配分配；`deployment/` 为部署工作流（准备模型 → 启动实例 → 验证实例）、启动器契约 `RuntimeLauncher` 与三个启动器（`fake.py`：控制平面进程内的 OpenAI 兼容桩服务 `FakeLauncher`；`vllm_process.py` / `tgi_process.py`：本机 vLLM / TGI 子进程 `VLLMProcessLauncher` / `TGIProcessLauncher`，共用 `process.py` 的 `ProcessLauncher`（启动、就绪检查、按会话停止），`process_records.py` 记录实例进程，下次启动时结束控制平面被杀死时留下的实例；由 `config.py` 按 `FORGEMESH_RUNTIME_LAUNCHER` 选择，`network.py` 为它们共用的地址校验与健康检查）、删除与重新部署之前的清理 `DeploymentReleaser`、`recovery.py` 中核对实例的 `check_instances`（实例已不在运行的 `Ready` 服务转为 `Degraded`，重启之后与控制器运行中定期调用）与回收孤立分配的 `reclaim_orphaned_allocations`，以及测试替身 `InMemoryLauncher`；`sqlite.py` 统一 SQLite 连接设置（WAL、外键），其中的 `Database`（一个连接加一把锁）供 Registry、Outbox 与审计日志共用，资源写入与它的事件、审计记录同一事务提交，并由 `workflow_db_path` 给出工作流存储文件；`events.py` 为资源变更事件的信封，`publishers.py` 为 Outbox 事件的发布器（默认写日志，`FORGEMESH_EVENT_WEBHOOK_URL` 选择 webhook），`pagetokens.py` 签发与校验列表分页游标（资源列表与审计记录列表共用 `registry_meta` 中的密钥），`version.py` 为控制平面版本；其余与任务书的 9 个子模块一一对应） |
| 核心契约 | 实现 `core-contracts` 的 `ResourceRegistry` / `Reconciler` / `Scheduler`（`FirstFitScheduler`：清单来自 `FORGEMESH_INVENTORY_FILE`，设置时才注入控制器；分配与设备占用经 `SQLiteRegistry.create_allocation` 在同一事务中写入，设备独占由 `allocation_devices` 部分唯一索引保证）；协调器按 phase 逐轮推进 `InferenceService`（`Pending` → `Scheduling` → `Preparing` → `Deploying` → `Verifying` → `Ready`；`spec` 变化时 `Ready` / `Degraded` 经 `Updating` 重新部署，`Failed` 回到 `Pending`；实例丢失的 `Degraded` 服务按退避重新部署），以 `deployment:{uid}:{generation}` 为幂等键启动部署工作流、经 `find_runs` 按前缀查询，成功后写入 `status.endpoints`，删除与重新部署之前经 `DeploymentReleaser` 取消部署、停止实例（部署包只依赖工作流契约，运行时与启动器由组装方注入：`app.py` 在设置了 `FORGEMESH_RUNTIME_LAUNCHER` 时以 `workflow-runtime` 的 `LocalWorkflowRuntime` 与所选启动器组装，未设置时不注入，协调器在分配之前报告 `WorkflowRuntimeUnavailable`）；`app.py` 提供 HTTP 服务（FastAPI，未安装时回退纯标准库占位——生产必须 FastAPI/ASGI），并在 lifespan 中运行控制器（设置了启动器时，先调用启动器的 `start()` 结束上次留下的实例进程，再恢复未结束的部署运行、把实例已不在运行的 `Ready` 服务转为 `Degraded`，然后启动控制器；控制器定期核对实例，在启动时与每次全量同步之后回收孤立的分配）；控制器按序号增量读取 `resource_changes` 变更记录，经 `update_status`（status 子资源，CAS）回写协调结果；API 的 `PUT` / `PATCH` 经 `update_spec`（期望状态，CAS，只有 `spec` 变化时递增 `generation`）写入，列表经 `list_page` 分页（游标绑定租户与过滤条件并签名）；Outbox 事务性事件（architecture.md 3.3 规则 7：Registry 对资源对象的每次写入在同一事务中写入资源变更事件，`app.py` 在 lifespan 中运行 `Dispatcher` 发布；API 的创建、更新与删除在同一事务中写入审计记录，经 `GET /api/v1/audit-events` 查询）；`ProcessedKeyStore` 去重（reconciler/dedup.py）；`PluginManager`（plugin_manager.py）注册时校验插件清单与 Ed25519 签名（受信任公钥来自插件策略文件），插件实际权限为清单申请与管理员授予的交集，控制平面应用尚未接入 |
| 运行形态 | 单实例控制平面进程（模块化单体，architecture.md 3.3 规则 8）；ADR-013 约束：仅允许单写方写同一 SQLite 权威库；控制器与 API 同进程运行，不得以多个 worker 进程启动 |
| 数据所有权 | **全部业务资源权威状态**（registry/store.py）、审计记录与 Outbox 事件（audit.py / outbox.py，与资源在同一个数据库文件、同一事务中写入）、任务与心跳状态 |
| 允许依赖 | `forgemesh.core` / `forgemesh.resource` / `forgemesh.policy` / `forgemesh.decision` / `forgemesh.workflow*` / `forgemesh.plugin`（plugin-sdk）、标准库 `sqlite3`、FastAPI |
| 禁止依赖 | 任何插件私有模块（`decision_rules` / `gpu_nvidia` 等，扫描二验证 ✅，见 3.2） |
| 测试路径 | `tests/unit/controlplane/`：`test_api.py`、`test_routes.py`、`test_registry.py`、`test_reconciler.py`、`test_dedup_store.py`、`test_plugin_manager.py`、`test_outbox.py`、`test_audit.py`、`test_tasks.py`、`test_heartbeat.py`、`test_observability.py`、`test_diagnostics.py`、`test_app.py`、`test_auth.py`、`test_controller_manager.py`、`test_controller_queue.py`、`test_registry_changes.py`、`test_registry_status.py`、`test_registry_allocations.py`、`test_sqlite.py`、`test_scheduler.py`、`test_scheduler_inventory.py`、`test_allocation_lifecycle.py`、`test_deployment.py`、`test_deployment_lifecycle.py`、`test_fake_launcher.py`、`test_vllm_launcher.py`、`test_tgi_launcher.py`、`test_process_records.py`、`test_launcher_config.py`、`test_launcher_network.py`、`test_registry_spec.py`、`test_api_updates.py`、`test_api_pagination.py`、`test_registry_events.py`、`test_registry_audit.py`、`test_audit_api.py`、`test_publishers.py`、`test_pagetokens.py`、`test_metrics.py`、`test_metrics_endpoint.py`、`test_telemetry.py`、`test_tracecontext.py`、`test_request_context.py`、`test_logs.py`（45 个文件，glob 核对；`test_publishers.py` 以本地 HTTP 服务覆盖 webhook 发布器；`exposition.py` 为指标测试共用的文本格式严格解析；`test_deployment_lifecycle.py` 以控制器、调度器、本地工作流运行时与内存启动器覆盖部署、更新、实例丢失后重新部署、重启、失败、回收孤立分配与删除闭环；`test_vllm_launcher.py` / `test_tgi_launcher.py` 以同目录的替身 `vllm_standin.py` / `tgi_standin.py` 代替 vLLM / TGI 运行，不需要 GPU；`test_process_records.py` 以 `launcher_host.py`（被 `SIGKILL` 的控制平面的替身）覆盖实例记录与留下的实例的清理）；端到端 `tests/e2e/test_closed_loop.py`（应用 + 假启动器：创建 → `Ready` → 对话 → 删除 → 重新创建 → 重启后经 `Degraded` 重新部署、回到 `Ready`） |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；WP-C1 补故障路径（CAS 并发、重启恢复、finalizer 闭环、Allocation 唯一性） |

### 2.6 Policy（policy-engine）

| 字段 | 内容 |
|---|---|
| 架构模块 | L2 策略层：配额、调度准入、QoS 评估（architecture.md 11.2） |
| 当前代码路径 | `packages/policy-engine/src/forgemesh/policy/{api,engine,rules,qos,quota}.py` |
| 核心契约 | `PolicyEngine(ABC)` / `PolicyAction` / `PolicyDecision`（api.py）；`DefaultPolicyEngine`（engine.py）；策略三态 `allow/deny/require_approval`（与 README v1.0.1 修正后口径一致）；配额与 QoS 评估（quota.py / qos.py） |
| 运行形态 | 进程内模块（被控制平面调用） |
| 数据所有权 | 无（纯评估；决策记录归 Decision 链路） |
| 允许依赖 | `forgemesh.core` / `forgemesh.resource`、pydantic |
| 禁止依赖 | 插件私有模块（扫描二验证 ✅）、DB 驱动、Web 框架 |
| 测试路径 | `tests/unit/policy/test_engine.py`、`test_qos.py`、`test_quota.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.7 Decision API（decision-api）

| 字段 | 内容 |
|---|---|
| 架构模块 | L3 Decision Router / JEV API（architecture.md 第 7 章） |
| 当前代码路径 | `packages/decision-api/src/forgemesh/decision/{provider,router,records,circuit_breaker,degradation}.py` |
| 核心契约 | `DecisionProvider(Protocol)` + `DecisionRequest` / `DecisionCandidate`（provider.py）；`DecisionRouter` + `DecisionResult`（router.py，路由层状态 `OK/FALLBACK_REQUIRED/FAILED`——与 Policy 三态是两层状态）；`DecisionRecord` / `DecisionRecordStore`（records.py，含 `approval_status` 审批字段，对齐 development-plan 5.5 / IVT-11）；熔断与降级（circuit_breaker.py / degradation.py） |
| 运行形态 | 进程内模块 |
| 数据所有权 | 决策记录的**契约与默认存储实现**同包（`DecisionRecordStore`，默认内存库）——见偏差 D1 |
| 允许依赖 | `forgemesh.core`（errors）、pydantic、标准库 `sqlite3`（**现状如此，边界见 D1**） |
| 禁止依赖 | 插件私有模块、Web 框架、LLM SDK、requests |
| 测试路径 | `tests/unit/decision/test_router.py`、`test_records.py`、`test_circuit_breaker.py`、`test_degradation.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；`records.py` 中契约/存储分离为有证据后的重构项（D1），不在 v1.1 强制 |

### 2.8 Decision Providers（插件 ×3）

| 字段 | 内容 |
|---|---|
| 架构模块 | L3/L4 决策能力插件：规则、JEV、类型安全 JEV |
| 当前代码路径 | `plugins/decision-rules/{provider.py,manifest.yaml}`；`plugins/decision-jev/{provider.py,manifest.yaml}`；`plugins/decision-typesafe-jev/{provider.py,manifest.yaml}`（PR #3 合入） |
| 核心契约 | 实现 `forgemesh.decision.provider.DecisionProvider`（Protocol）；manifest 受 `api/schemas/plugin-manifest.schema.json` 约束 |
| 运行形态 | 插件（进程内运行，无进程隔离，见 D3；manifest `entrypoint` 由 `forgemesh.plugin.loader` 加载） |
| 数据所有权 | 无（候选产出到 DecisionRouter；记录归 DecisionRecordStore） |
| 允许依赖 | `forgemesh.decision`（API 包）、plugin-sdk |
| 禁止依赖 | 控制平面内部实现（扫描五验证 ✅，见 3.5）、其他插件私有数据（architecture.md 3.3 规则 4） |
| 测试路径 | `tests/unit/plugins/test_decision_rules.py`、`test_decision_jev.py`、`test_decision_typesafe_jev.py`；插件 SDK 的签名、签名工具、权限词表、入口加载与信任级别检查见同目录 `test_signing.py`、`test_signtool.py`、`test_permissions.py`、`test_loader.py`、`test_isolation.py`；全部插件的入口加载另由 CI `scripts/plugin_entrypoint_smoke.py` 冒烟 |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；WP-E1 conformance（超时/无效输出/降级路径） |

### 2.9 Workflow API（workflow-api）

| 字段 | 内容 |
|---|---|
| 架构模块 | WorkEngine API（architecture.md 第 6 章，ADR-003） |
| 当前代码路径 | `packages/workflow-api/src/forgemesh/workflow/{runtime,states}.py` |
| 核心契约 | `WorkflowRuntime(ABC)` + `WorkflowDefinition` / `Step` / `RetryPolicy` / `RunRef` / `RunInfo`（运行快照：状态、步骤状态、步骤输出、失败原因；`get_run` / `find_run` / `find_runs` 为非抽象方法，`get_run` 默认只返回状态，后两者默认抛 `NotImplementedError`）（runtime.py）；`WorkflowStatus` / `StepStatus` 状态机与 `validate_workflow_transition`（states.py） |
| 运行形态 | 进程内库（抽象层，实现见 2.10） |
| 数据所有权 | 无 |
| 允许依赖 | `forgemesh.core`、pydantic |
| 禁止依赖 | 插件、DB 驱动（`sqlite3` 等）、Web 框架、LLM SDK（扫描一验证 ✅，见 3.1） |
| 测试路径 | `tests/unit/workflow/test_states.py`、`test_runtime_contract.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.10 Workflow Runtime（workflow-runtime）

| 字段 | 内容 |
|---|---|
| 架构模块 | 工作流执行引擎（ADR-003 内部结构：DSL / 执行 / 存储 / 审批 / 补偿） |
| 当前代码路径 | `packages/workflow-runtime/src/forgemesh/workflowruntime/{dsl,exec,store,approval,compensate,gpu_onboarding,runtime}.py` |
| 核心契约 | `LocalWorkflowRuntime`（runtime.py）实现 `WorkflowRuntime(ABC)`：在后台执行运行、失败时反向补偿、`recover()` 让重启前未结束的运行继续、`aclose()` 结束全部后台任务；DSL 解析（dsl.py）、步骤执行与重试（exec.py）、运行存储（store.py）、审批集成（approval.py）、补偿语义（compensate.py）；GPU onboarding 内置工作流（gpu_onboarding.py） |
| 运行形态 | 进程内模块（被控制平面编排调用） |
| 数据所有权 | 工作流运行实例状态（store.py，含步骤结果与失败原因；恢复/重启语义见 performance 测试与 `test_runtime.py`）；部署工作流的运行存放在控制平面 `workflow_db_path` 给出的独立文件中（设置了 `FORGEMESH_RUNTIME_LAUNCHER` 时由控制平面应用打开） |
| 允许依赖 | `forgemesh.workflow`（API）、`forgemesh.core`、`forgemesh.resource`、标准库 |
| 禁止依赖 | Workflow Provider 插件私有模块（扫描六验证 ✅，见 3.5）、直连插件 |
| 测试路径 | `tests/unit/workflowruntime/test_{dsl,exec,store,approval,compensate,runtime}.py`；端到端 `tests/e2e/test_gpu_onboarding.py`；恢复语义 `tests/performance/test_restart_recovery.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；WP-C2 补长步骤超时/断联/取消补偿/版本固定等混沌场景 |

### 2.11 Workflow Providers（插件 ×2）

| 字段 | 内容 |
|---|---|
| 架构模块 | L4 工作流能力插件 |
| 当前代码路径 | `plugins/workflow-reference/{provider.py,manifest.yaml}`；`plugins/workflow-temporal/{provider.py,manifest.yaml,__init__.py}` |
| 核心契约 | 实现 `WorkflowRuntime(ABC)`（作为可插拔后端）：`workflow-reference` 是 `LocalWorkflowRuntime` 的子类；`workflow-temporal` 沿用 `get_run` / `find_run` / `find_runs` 的默认实现（快照不含步骤输出、不能按幂等键或其前缀查找），不能执行部署工作流；manifest 同 2.8 |
| 运行形态 | 插件（进程内加载；temporal 为本地语义实现，生产 Temporal 集群对接待核验） |
| 数据所有权 | 无（运行权威状态在 runtime/store 或其自身后端内） |
| 允许依赖 | `forgemesh.workflow`、plugin-sdk |
| 禁止依赖 | 控制平面内部实现（扫描五验证 ✅）、其他插件私有数据 |
| 测试路径 | `tests/unit/plugins/test_workflow_reference.py`、`test_workflow_temporal.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；`workflow-temporal` 的真实 Temporal 服务对接属"实现待核验"范围，留待有需求证据后处理 |

### 2.12 Node Agent

| 字段 | 内容 |
|---|---|
| 架构模块 | L5 执行平面节点代理（architecture.md 第 10 章） |
| 当前代码路径 | `packages/node-agent/src/forgemesh/nodeagent/{server,probe,runtime,health,recover}.py`；协议 `api/protobuf/agent.proto`（proto3 `forgemesh.agent.v1alpha1` 草案） |
| 核心契约 | `AgentServer` / `AgentClient`（server.py）；有类型任务协议（ADR-009：禁止 RunShell/EXEC）、`idempotency_key` 去重、`correlation_id` 贯穿（INV-4）；deadline 以 RFC 3339 字符串承载 |
| 运行形态 | 独立进程（节点侧，接受控制平面下发的受控任务） |
| 数据所有权 | 节点本地运行态（probe 结果、健康、恢复状态）；资源权威状态仍归控制平面 |
| 允许依赖 | `forgemesh.core`、标准库；与控制平面仅经 gRPC/HTTP 协议交互 |
| 禁止依赖 | `forgemesh.controlplane` 任何内部实现（扫描三验证 ✅，见 3.3） |
| 测试路径 | `tests/unit/nodeagent/test_{server,probe,runtime,health,recover}.py` |
| 状态 | 已实现且有测试覆盖；proto 仍为 `v1alpha1` 草案（正式化属演进项） |
| 演进动作 | 保留；proto 转正式版时可替换 timestamp 类型（见 agent.proto 头注释） |

### 2.13 Hardware Provider（gpu-nvidia）

| 字段 | 内容 |
|---|---|
| 架构模块 | L4 硬件能力插件：发现 / 预检 / 健康 / MIG / 异构（architecture.md 9.2 / 9.4） |
| 当前代码路径 | `plugins/gpu-nvidia/{discovery,preflight,health,mig,hetero}.py` + `manifest.yaml`（5 个子模块文件，glob 核对） |
| 核心契约 | GPU 发现与能力上报（discovery）、驱动/运行时预检（preflight）、健康监测（health）、MIG 切分（mig）、异构设备（hetero）；manifest 同 2.8 |
| 运行形态 | 插件（由控制平面经 Node Agent/Provider 接口调用，进程内运行，无进程隔离） |
| 数据所有权 | 无（上报数据归 Registry；设备实际状态归节点） |
| 允许依赖 | plugin-sdk、`forgemesh.resource`（设备模型） |
| 禁止依赖 | 控制平面内部实现、其他插件私有数据 |
| 测试路径 | `tests/unit/plugins/test_gpu_nvidia.py`、`test_gpu_mig.py`、`test_gpu_hetero.py`；端到端 `tests/e2e/test_gpu_onboarding.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.14 Runtime Providers（插件 ×2）

| 字段 | 内容 |
|---|---|
| 架构模块 | L4 推理运行时插件：vLLM、TGI |
| 当前代码路径 | `plugins/runtime-vllm/{deploy,model,release,verify}.py` + `manifest.yaml`（入口 `deploy:VLLMDeployer`）；`plugins/runtime-tgi/{provider.py,manifest.yaml,__init__.py}` |
| 核心契约 | Runtime Provider 接口（部署 deploy、模型加载 model、发布 release、验证 verify——vllm 分四个模块；tgi 精简 provider）；manifest 同 2.8 |
| 运行形态 | 插件（产出/操作推理服务实例；实际推理流量不经此，经 Go 网关直达）；控制平面部署推理服务不经这两个插件：启动器（含 `vllm-process` / `tgi-process`）是控制平面 `deployment/` 中的代码（见 2.5；控制平面不导入插件，见扫描二） |
| 数据所有权 | 无（InferenceService 权威状态归 Registry） |
| 允许依赖 | plugin-sdk、`forgemesh.resource` |
| 禁止依赖 | 控制平面内部实现（扫描五验证 ✅）、其他插件私有数据 |
| 测试路径 | `tests/unit/plugins/test_runtime_vllm.py`、`test_runtime_tgi.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.15 QoS / Metering（插件 ×2）

| 字段 | 内容 |
|---|---|
| 架构模块 | L4 服务质量与计量插件（architecture.md 11.2–11.3：配额/调度/QoS/计量分离、Token 计量） |
| 当前代码路径 | `plugins/qos-basic/{provider.py,manifest.yaml}`；`plugins/metering-basic/{provider.py,usage.py,manifest.yaml}` |
| 核心契约 | QoS 评估接口（与 policy-engine 的 qos.py 分层：policy 定判据、插件给实现）；usage 计量（Token 级）；manifest 同 2.8 |
| 运行形态 | 插件 |
| 数据所有权 | 无（计量流水由其产出、控制平面/网关侧落账） |
| 允许依赖 | plugin-sdk、`forgemesh.resource` |
| 禁止依赖 | 控制平面内部实现、其他插件私有数据 |
| 测试路径 | `tests/unit/plugins/test_qos_basic.py`、`test_metering_basic.py` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留 |

### 2.16 持久化（SQLite-first，ADR-013）

| 字段 | 内容 |
|---|---|
| 架构模块 | L6 权威存储（过渡态：SQLite；目标态：PostgreSQL） |
| 当前代码路径 | `packages/control-plane/src/forgemesh/controlplane/registry/store.py`（`SQLiteRegistry`，权威资源状态，CAS 乐观并发 `resource_version`；分配 `allocations` 与设备占用 `allocation_devices`，部分唯一索引保证每个资源 uid 在其租户内至多一个 Active 分配、每个设备（跨租户）至多属于一个 Active 分配；`registry_meta` 保存列表分页游标的签名密钥（由 `pagetokens.py` 创建，审计记录列表共用），随库备份与恢复）；`packages/decision-api/src/forgemesh/decision/records.py`（`DecisionRecordStore`）；`packages/workflow-runtime/src/forgemesh/workflowruntime/store.py`（工作流运行存储）；审计 `audit.py` 与 Outbox `outbox.py`（与 Registry 共用连接） |
| 核心契约 | ADR-013：**SQLite 为权威存储、单控制平面实例写、禁止扩展 replicas；PostgreSQL 为目标态**（迁移由真实 SLO/基准触发）；Registry 对齐 `core-contracts.ResourceRegistry` 契约 |
| 运行形态 | 进程内嵌入式：控制平面应用打开 `FORGEMESH_DB_PATH`（Registry、Outbox 事件、审计记录与去重键，WAL 模式）；设置了 `FORGEMESH_RUNTIME_LAUNCHER` 时另以 `workflow_db_path(FORGEMESH_DB_PATH)` 给出的同目录独立文件（`*.workflows.db`，WAL 模式，步骤写入不必等待 Registry 的写锁）作为部署工作流的存储；决策记录、工作流存储与单独构造的审计日志未指定文件时使用内存库。两个文件之间没有跨文件事务：备份时先注册表、后工作流存储，同一组恢复，见 docs/operations/backup-restore.md |
| 数据所有权 | 权威写方：控制平面**唯一实例**（registry/audit/outbox）；决策记录写方为 Decision 链路（D1 关联） |
| 允许依赖 | 标准库 `sqlite3`（仅控制平面实现层；API 层现状见 D1） |
| 禁止依赖 | 第三方 DB 驱动（v1.x）、多实例并发写同一库文件 |
| 测试路径 | `tests/unit/controlplane/test_registry.py`、`tests/unit/controlplane/test_registry_allocations.py`、`tests/unit/controlplane/test_registry_events.py` / `test_registry_audit.py`（资源写入与事件、审计记录同一事务）、`tests/unit/decision/test_records.py`、`tests/unit/workflowruntime/test_store.py`；恢复/并发：`tests/performance/test_restart_recovery.py`、`test_conflict_correctness.py`、`tests/chaos/test_cp_restart.py`、`tests/unit/workflowruntime/test_runtime.py`、`tests/unit/controlplane/test_deployment_lifecycle.py`；应用打开两个文件（`tests/unit/controlplane/test_app.py`）、重启后服务转为 `Degraded` 并重新部署（`tests/e2e/test_closed_loop.py`）；备份恢复演练见 `docs/operations/backup-restore.md` |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；WP-C1 补 CAS 并发冲突/重启恢复/备份恢复的故障路径测试；PostgreSQL 迁移属阶段 F（有证据后） |

### 2.17 开发与 CI

| 字段 | 内容 |
|---|---|
| 架构模块 | 开发工具链与持续集成 |
| 当前代码路径 | `Makefile`（target：`install` / `deps` / `test` / `test-unit` / `test-contract` / `lint` / `fmt` / `clean`，实测核对）；根 `pyproject.toml`（**仅 ruff/pytest/coverage 工具配置，不含打包配置**——monorepo + PEP 420 命名空间包，各子包自带 pyproject）；`.github/workflows/ci.yml`（四 job：`python` 矩阵 lint+pytest（未 await 的协程视为失败）、`go` gofmt+vet+build+`go test -race`、`compose-smoke` 构建镜像并以假启动器端到端冒烟推理服务闭环、`architecture-docs` 校验 OpenAPI/Schema/ADR）；`requirements-dev.txt`；根 `conftest.py`；`gateway/Makefile` |
| 核心契约 | 子包统一模板（README「统一子包模板」+ CONTRIBUTING）；`requires-python >= 3.12` 基线（WP-A1.3 统一） |
| 运行形态 | 开发期工具 + CI 流水线（push 触发，`ci-${{ github.ref }}` 分组去重） |
| 数据所有权 | 无 |
| 允许依赖 | 开发依赖（ruff/pytest/coverage/jsonschema 等） |
| 禁止依赖 | — |
| 测试路径 | CI 即验证载体；本地基线 `python -m pytest tests/ -q` → 993 passed（2026-10-11） |
| 状态 | 已实现且验证（Python 侧本地全绿；Go 侧以 CI `go` job 为准，本机无工具链） |
| 演进动作 | 保留；WP-A2 计划补强：ADR 动态校验、manifest 实例校验、OpenAPI 语义校验、元数据一致性、插件入口冒烟 |

### 2.18 部署与运维

| 字段 | 内容 |
|---|---|
| 架构模块 | 部署拓扑与运维文档（L6 平台基础设施落地） |
| 当前代码路径 | `deploy/docker/{Dockerfile.control-plane,Dockerfile.gateway}`（均以非 root 用户运行：控制平面 uid 10001，网关 distroless `nonroot`）与根目录 `.dockerignore`；`deploy/docker/docker-compose.yaml`（control-plane + gateway，只发布在 127.0.0.1；postgres 在 `postgres` profile 中、默认不启动；含 ADR-013 单实例注记；不设置加速器清单与启动器，不部署推理实例）与叠加文件 `deploy/docker/docker-compose.fake-runtime.yaml`（一块虚构设备的清单 `deploy/docker/fake-runtime-inventory.json` + 假启动器，不需要 GPU 的推理服务闭环）；`deploy/kubernetes/{namespace,control-plane-deployment}.yaml`（replicas: 1、`strategy: Recreate`、数据库在 PVC 上、key 文件来自 Secret `forgemesh-api-keys`；不设置加速器清单与启动器，不部署推理实例）；`scripts/compose_smoke.sh`；`docs/operations/backup-restore.md`（RPO≤5min / RTO≤15min、在线备份与恢复演练）；示例 `examples/single-node/{inference-service,plugin-vllm}.yaml`、`examples/multi-tenant/tenant-quota.yaml` |
| 核心契约 | ADR-013 单控制平面实例约束：compose 不扩展 control-plane；k8s 清单 `replicas: 1` 且更新策略为 `Recreate`（旧 Pod 退出后才启动新 Pod，更新期间不会有两个写者）；控制平面只使用 SQLite，postgres 为目标态预置 |
| 运行形态 | 容器镜像（Dockerfile）+ 部署工件（YAML）+ 运维文档（Markdown） |
| 数据所有权 | 无；控制平面的 SQLite 库（叠加 `docker-compose.fake-runtime.yaml` 时还有工作流存储 `forgemesh.workflows.db`）位于 compose 卷 `control-plane-data` / k8s PVC `forgemesh-control-plane-data`（pgdata 卷属目标态预置，仅 `postgres` profile 使用） |
| 允许依赖 | — |
| 禁止依赖 | —（约束：部署不得暗示多副本写同一 SQLite） |
| 测试路径 | `scripts/compose_smoke.sh`（CI `compose-smoke` job：构建镜像，以两个 compose 文件启动，经网关验证无 key 返回 401，创建 `InferenceService` → `Ready` → 经网关流式对话 → 不带凭证抓取两边的 `/metrics`，带 `traceparent` 的请求在控制平面的错误信封与两边的日志中带同一个 trace-id → 删除（实例端口关闭）→ 同名重新创建并再次对话 → `PUT` 修改 `spec` → `Updating` → 以新实例回到 `Ready` 并再次对话 → 让假实例退出 → `Degraded`（`InstanceLost`）→ 重新部署、回到 `Ready` 并再次对话，控制平面的 `/metrics` 计入定期核对发现的丢失与重新部署 → 重启控制平面：资源仍在，服务经 `Degraded`（`InstanceLost`）重新部署、回到 `Ready` 并再次对话，`/metrics` 计入启动时核对发现的丢失与重新部署 → 删除）；k8s 清单无自动化测试；备份恢复有人工演练清单 |
| 状态 | compose 已实现且有测试覆盖；k8s 清单实现待核验（无自动化测试，尚未提供网关清单、ServiceAccount、NetworkPolicy）；运维文档已交付 |
| 演进动作 | k8s 补网关清单、ServiceAccount 与 NetworkPolicy；A1.5 已核对部署文档含单实例约束；目标态 PostgreSQL 部署属阶段 F |

### 2.19 命令行工具（cli）

| 字段 | 内容 |
|---|---|
| 架构模块 | L1 Access Layer 的命令行入口（architecture.md 3.1 L1） |
| 当前代码路径 | `packages/cli/src/forgemesh/cli/{main,commands,client,config,documents,errors,output}.py`（共 7 个 `.py`，glob 核对；发行名 `forgemesh-cli`，控制台脚本 `forgemesh`） |
| 核心契约 | 经 `/api/v1/inference-services` 管理 `InferenceService`：`apply`（`GET` 之后 `POST`，或以读到的 `resourceVersion` / `uid` 为前置条件 `PUT`，`409` 时重新读取再试）、`get`、`describe`、`list`（跟随 `nextPageToken`）、`delete`（受控删除，区分 `204` / `202`）、`wait`（`Ready`、`Ready=True` 且 `observedGeneration` 等于 `generation`，或资源消失）；地址取自 `--api-url` / `FORGEMESH_API_URL`（默认网关 `http://127.0.0.1:8081`），key 取自 `--api-key-file` / `FORGEMESH_API_KEY`；错误响应（错误信封，或反向代理等返回的非信封响应）按状态码映射为按原因区分的退出码（`errors.py` 的 `ExitCode`），只有错误码为 `NOT_FOUND` 的 `404` 表示资源不存在 |
| 运行形态 | 命令行进程，HTTP 客户端（标准库 `urllib`，不跟随重定向） |
| 数据所有权 | 无（资源权威状态归控制平面 Registry，CLI 不在本地保存状态） |
| 允许依赖 | 标准库、PyYAML（`pyproject.toml` 只声明 `pyyaml>=6.0.1`）；与控制平面仅经 HTTP API 交互 |
| 禁止依赖 | `forgemesh.controlplane` 及其他 ForgeMesh 包、直接打开 SQLite（扫描七验证 ✅，见 3.7） |
| 测试路径 | `tests/unit/cli/`：`test_commands.py`、`test_client.py`、`test_config.py`、`test_documents.py`、`test_packaging.py`（5 个测试文件，glob 核对；`fakes.py` 以本地 HTTP 服务器模拟 API，`conftest.py` 提供夹具）；端到端 `tests/e2e/test_cli.py`（uvicorn 运行的控制平面应用 + API key + 假启动器：`apply` → `wait` → 修改 `spec` 后 `apply` → `wait`（第 2 代 `spec`）→ `get` → `list` → `delete` → `wait --for delete`，以及 API 错误的退出码） |
| 状态 | 已实现且有测试覆盖 |
| 演进动作 | 保留；只管理 `InferenceService`，其他 kind 待 API 提供写接口后再加 |

---

## 3. 依赖边界证据（import 扫描循证）

> 以下每条扫描均在本分支实际执行（2026-10-11，macOS/zsh，`grep -rn --include="*.py"`，排除 `egg-info`；`grep` 无命中时退出码 1，以 `EXIT=` 标注）。结论仅基于命令输出，不引用 README 断言。

### 3.1 扫描一：契约包不得依赖插件 / DB 驱动 / Web 框架 / LLM SDK

```bash
grep -rn "import sqlite3\|from fastapi\|import requests\|plugins\." \
  packages/core-contracts/src/ packages/workflow-api/src/ packages/decision-api/src/ \
  --include="*.py" | grep -v egg-info | grep -v test
```

**输出（命中 1 条）：**

```
packages/decision-api/src/forgemesh/decision/records.py:16:import sqlite3
```

**结论：⚠️ 违规需处理（1 处）** —— `core-contracts` 与 `workflow-api` 干净（零命中）；`decision-api` 的 `records.py` 导入标准库 `sqlite3`：该文件同时含契约模型 `DecisionRecord` 与 SQLite 存储实现 `DecisionRecordStore`，即"决策记录契约"与"默认存储实现"未分包。sqlite3 为标准库而非第三方驱动，属**边界设计偏差**而非外部依赖泄漏，处置见 D1。

### 3.2 扫描二：核心包不得导入插件私有模块

```bash
grep -rn "decision_rules\|decision_jev\|gpu_nvidia\|runtime_vllm\|workflow_reference\|workflow_temporal" \
  packages/control-plane/src/ packages/policy-engine/src/ --include="*.py" | grep -v egg-info
```

**输出：未命中（EXIT=1）**

**结论：✅ 符合** —— 控制平面与策略引擎对 10 个插件零私有耦合；插件接入仅经 manifest/entrypoint 的公共契约（入口类由 `forgemesh.plugin.loader` 按插件目录加载，不经 import 插件模块名；`DecisionProvider` 等 Protocol/ABC）。

### 3.3 扫描三：Node Agent 不得导入控制平面内部实现

```bash
grep -rn "controlplane" packages/node-agent/src/ --include="*.py" | grep -v egg-info
```

**输出：未命中（EXIT=1）**

**结论：✅ 符合** —— `node-agent`（server/probe/runtime/health/recover 共 5 文件）与控制平面仅经 agent.proto 协议交互，无进程内耦合。

### 3.4 扫描四：gateway/ 为纯 Go（不 import Python）

```bash
ls gateway/*.go gateway/cmd/... 2>/dev/null | head   # 根级无 *.go，代码在 cmd/ 与 internal/
find gateway -name "*.go" | wc -l                     # → 53
find gateway -name "*.py" -o -name "requirements*.txt" | wc -l   # → 0
cat gateway/go.mod                                     # module github.com/Mekoforge/ForgeMesh/gateway (go 1.22)
```

**输出：** 53 个 `.go`（含 31 个 `_test.go`）、0 个 `.py`、无 Python 依赖清单；`go.mod` 无任何第三方 require（仅标准库 + go 1.22）。

**结论：✅ 符合** —— 网关与 Python 控制平面为进程边界隔离（HTTP 上游），无语言互操作。

### 3.5 补充扫描五：插件不得反向依赖控制平面内部实现

```bash
grep -rn "from forgemesh.controlplane\|import forgemesh.controlplane\|controlplane\." \
  plugins/*/provider.py plugins/*/*.py | grep -v pycache
```

**输出：未命中（EXIT=1）**

**结论：✅ 符合** —— 插件只面向 API 包（`forgemesh.decision` / `forgemesh.workflow` 等）编程，单向依赖成立（核心→契约←插件）。

### 3.6 补充扫描六：workflow-runtime / resource-model 不得导入插件私有模块

```bash
grep -rn "decision_rules\|gpu_nvidia\|runtime_vllm\|workflow_temporal" \
  packages/workflow-runtime/src/ packages/resource-model/src/ --include="*.py" | grep -v egg-info
```

**输出：未命中（EXIT=1）**

**结论：✅ 符合** —— 运行时与资源模型层亦无插件私有耦合。

### 3.7 补充扫描七：CLI 只经 HTTP API 访问控制平面

2026-10-13 于本分支执行（Linux/bash）：

```bash
grep -rnE "^\s*(from|import)\s+forgemesh\b" packages/cli/src/ --include="*.py" | grep -v egg-info
grep -rhoE "^\s*(from|import)\s+[A-Za-z_][A-Za-z0-9_.]*" packages/cli/src/ --include="*.py" \
  | sed -E 's/^\s*(from|import)\s+//' | cut -d. -f1 | sort -u
```

**输出：** 第一条未命中（EXIT=1，包内模块之间只用相对导入）；第二条只列出标准库模块与 `yaml`：`argparse`、`collections`、`copy`、`dataclasses`、`email`、`enum`、`__future__`、`http`、`json`、`math`、`os`、`pathlib`、`re`、`sys`、`time`、`typing`、`urllib`、`uuid`、`yaml`。

**结论：✅ 符合** —— CLI 是 REST API 的客户端，不导入控制平面或其他 ForgeMesh 包，与控制平面之间只有 HTTP 边界。`tests/unit/cli/test_packaging.py` 在新的解释器中导入 CLI 入口，核对新导入的第三方发行包与 `pyproject.toml` 声明的依赖完全一致（`pyyaml`），且没有导入其他 ForgeMesh 包。

### 3.8 依赖边界结论汇总

| # | 边界规则 | 结果 |
|---|---|---|
| 1 | 契约包（core-contracts / workflow-api / decision-api）不依赖插件/DB 驱动/Web 框架/LLM SDK | ⚠️ 1 处偏差（decision-api `sqlite3`，见 D1） |
| 2 | 核心包（control-plane / policy-engine）不导入插件私有模块 | ✅ 符合 |
| 3 | node-agent 不导入控制平面内部实现 | ✅ 符合 |
| 4 | gateway 纯 Go、零 Python 依赖 | ✅ 符合 |
| 5 | 插件不反向依赖控制平面内部实现 | ✅ 符合 |
| 6 | workflow-runtime / resource-model 不导入插件私有模块 | ✅ 符合 |
| 7 | cli 不导入控制平面或其他 ForgeMesh 包，只经 HTTP API 访问 | ✅ 符合 |

**总计：7 条边界规则中 6 条完全符合，1 条存在 1 处偏差（D1，记录不修复）。**

---

## 4. 发现的偏差（只记录，不在本任务修复源码）

| 编号 | 偏差 | 证据 | 影响 | 建议处置 |
|---|---|---|---|---|
| **D1** | 决策记录的契约与 SQLite 存储实现共存于 API 包：`decision-api` 导入 `sqlite3`，违反"契约包不依赖 DB 驱动"的边界规则 | 扫描一命中：`packages/decision-api/src/forgemesh/decision/records.py:16`（`DecisionRecord` + `DecisionRecordStore` 同文件） | 边界纯度受损；存储演进（如 PostgreSQL 迁移）将波及 API 包；属标准库依赖，无外部供应链风险 | **有证据后重构**：将 `DecisionRecordStore` 迁至控制平面实现层（与 `SQLiteRegistry` 同域），API 包仅保留 `DecisionRecord` 模型与存储接口；需 ADR 评审与迁移测试，不属 v1.1 强制项 |
| **D3** | ADR-006 的受限 RPC 宿主未实现：插件（包括第三方插件）都在宿主进程内运行，没有进程隔离 | `packages/plugin-sdk/src/forgemesh/plugin/manifest.py` 的 `SUPPORTED_ISOLATION` 只有 `in-process`（Schema 的 `isolation` 枚举相同）；同目录 `loader.py` 的 `load_entrypoint` 在当前进程内导入插件包；`IsolationMode.RESTRICTED_RPC` 只用于 `IsolationGuard` 的权限检查，没有对应的宿主实现 | 插件代码拥有宿主进程的全部能力（网络、文件系统、数据库连接），权限授予与 `check_permissions` 约束不了进程内的插件代码，只能靠注册时的签名校验限定插件来源 | **实现进程边界或修订 ADR-006**：受限 RPC 宿主实现之前，清单不得声明 `restricted-rpc` / `sandbox`，只注册可信来源的签名插件 |

> 备注（不计入偏差）：`agent.proto` 为 `v1alpha1` 草案（timestamp 用 RFC 3339 字符串），正式化时机由演进决定。

---

## 5. 与架构分层（L1–L6）的对应关系

对照 architecture.md 3.1 的六个逻辑层次：

| 层次 | 包含的映射模块（本文编号） |
|---|---|
| L1 Access Layer | 1（外部 API 契约）、2（Go 网关）、19（命令行工具） |
| L2 Core Control Plane | 3（核心契约）、4（资源模型）、5（控制平面）、6（Policy）、16（持久化） |
| L3 Orchestration & Decision | 7（Decision API）、8（Decision Providers）、9（Workflow API）、10（Workflow Runtime）、11（Workflow Providers） |
| L4 Plugin Capability Layer | 8、11、13（GPU）、14（Runtime）、15（QoS/Metering） |
| L5 Execution Plane | 12（Node Agent）、14（推理服务实际承载） |
| L6 Platform Infrastructure | 16（SQLite/目标态 PostgreSQL）、17（CI）、18（部署运维） |

---

## 6. 维护约定

1. **触发更新**：新增/迁移包目录、插件增删、持久化引擎变更（ADR-013 演进）、契约版本变化（openapi/proto）时，必须同步更新本文对应小节并重跑第 3 节扫描。
2. **状态判级**：不得仅凭 README/CHANGELOG 断言"已验证"；升级为"已实现且有测试覆盖"须给出测试路径证据。
3. **偏差闭环**：D1/D2 处置后，从第 4 节移除并在 CHANGELOG 记录；新偏差先入表、后排期，禁止静默修复无记录。