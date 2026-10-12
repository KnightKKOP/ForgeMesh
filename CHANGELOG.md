# Changelog

本文件记录 ForgeMesh 所有显著变更，遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/) 规范。

## [1.1.0] - Unreleased

### Added

- SQLite 单实例部署约束文档：`deploy/docker/docker-compose.yaml` 顶部注释（ADR-013：仅允许单控制平面实例写同一权威库，不得扩展 replicas）与 [docs/operations/backup-restore.md](docs/operations/backup-restore.md) 新增"单实例写入约束"小节（含升级流程：停写 → 备份 → 升级 → 恢复）。
- 新增 [docs/architecture/v1.1-development-plan.md](docs/architecture/v1.1-development-plan.md)（阶段 A–E 可信基线计划）与 [docs/architecture/module-mapping.md](docs/architecture/module-mapping.md)（逻辑模块 ↔ 真实代码路径映射，含依赖边界 import 循证）。
- 新增 CI 校验脚本：`scripts/validate_manifests.py`（JSON Schema metaschema 自校验 + 10 插件 manifest 实例校验）、`scripts/openapi_semantic_check.py`（operationId 唯一 / $ref 可解析 / 响应完整性）、`scripts/check_metadata_consistency.py`（requires-python>=3.12、描述禁 skeleton/placeholder）、`scripts/plugin_entrypoint_smoke.py`（插件入口进程内冒烟）。
- 新增契约 / 混沌 / 兼容测试 122 项（1115 vs 993）：Registry CAS 并发 / 租户过滤 / 重启恢复（19）、Workflow 恢复版本固定 / 取消补偿 + Outbox 事务 / 幂等 / 死信（12）、10 插件发布链路 conformance——manifest/isolation 一致性 + 四类 Provider 契约 + 卸载安全（91）。
- 控制平面可运行入口：`control-plane` 包新增控制台脚本 `forgemesh-control-plane`（`forgemesh.controlplane.app:main`）；权威存储路径取自 `FORGEMESH_DB_PATH`（默认 `forgemesh.db`），监听地址取自 `FORGEMESH_HOST` / `FORGEMESH_PORT`（默认 `127.0.0.1:8080`），启动方式见 README「运行控制平面」。
- 新增 `tests/contract/test_registry_hardening.py`：并发创建全部持久化、空 namespace 身份唯一、删除 / 释放 CAS、标签过滤与分页等 Registry 回归测试。
- 新增 [ADR-014](docs/design-decisions/adr-014.md)（Accepted）：对外 API 采用绑定租户的 API Key 认证。
- API Key 认证（ADR-014）：网关（`GATEWAY_AUTH_MODE` / `GATEWAY_API_KEYS_FILE`）与控制平面（`FORGEMESH_AUTH_MODE` / `FORGEMESH_API_KEYS_FILE`）各自校验 `Authorization: Bearer <key>`；key 文件只保存 SHA-256 摘要，每个 key 绑定租户、主体与 scope（`read` / `write` / `*`）；`python -m forgemesh.controlplane.keygen --tenant <租户> --subject <主体>` 生成 key 与对应的 key 文件条目。
- 网关 `make run-dev`：以 `dev` 认证模式在 `127.0.0.1:8081` 启动，仅供本地开发。
- OpenAPI：新增 `apiKey`（HTTP bearer）安全方案并设为全局 `security`（探针豁免），全部 `/api/v1` 端点声明 `401` / `403`。
- 控制器：控制平面在应用启动（lifespan）时运行 `ControllerManager`，自动协调 `InferenceService` 并回写 `status`。Registry 每次写入时在同一事务中追加一条 `resource_changes` 变更记录，控制器按序号增量读取并清理已读记录；启动时全量重建工作队列，另每 10 分钟全量同步一次。工作队列按资源去重、支持延迟重试，失败时指数退避；`status` 以 `resource_version` 做 CAS 写入，去重键在写入成功之后才登记。控制器是单写者（ADR-013），应用只能以单进程运行。
- Registry status 子资源写入 `update_status`：只修改 `conditions` / `phase` / `observedGeneration`，不递增 `generation`；条件按 `type` 合并，`status` 不变时保留 `lastTransitionTime`；支持以 `resource_version` 做 CAS，未建模的 payload 字段原样保留。另新增 `get_with_phase`、`list_resource_keys`、`changes_since`、`latest_change_seq`、`prune_changes`。
- `ReconciliationController` 新增 `record_dedup_keys` 参数与 `mark_processed()`：调用方可以在持久化 `status` 之后再登记去重键。
- `InferenceServiceStatus.endpoints`：每个实例包含 `url`（http/https 基地址，不含凭证、查询串与片段）、`ready`（默认 `true`）、可选的 `instanceId`，以及必填的 `protocol`（`openai-chat` / `tgi-generate`），资源模型、JSON Schema 与 OpenAPI 一致；`update_status` 可以整体替换 `endpoints`，写入前逐项校验。
- 网关新增带命名空间的推理路由 `/api/v1/namespaces/{ns}/inference/{svc}/chat`：Resolver 以 `?namespace=` 查询控制平面，缓存按租户与命名空间隔离；`/api/v1/inference/{svc}/chat` 继续解析不带命名空间的服务。
- 网关请求体上限 `GATEWAY_MAX_REQUEST_BODY_BYTES`（默认 10 MiB）：声明的 `Content-Length` 超限时直接返回 `413 REQUEST_TOO_LARGE`，不访问上游；分块传输的请求体读到上限时同样返回 `413`，不计实例失败。
- 容器镜像：`deploy/docker/Dockerfile.control-plane`（控制平面，以 uid 10001 运行）与 `deploy/docker/Dockerfile.gateway`（静态编译的网关，distroless `nonroot` 用户）；根目录 `.dockerignore` 只把 `packages/` 与 `gateway/` 送入构建上下文。
- `scripts/compose_smoke.sh` 端到端冒烟：构建镜像并按 compose 启动，经网关验证无 key 返回 `401`、创建与读取推理服务、控制器写入 `status`，重启控制平面后资源仍在。CI 新增 `compose-smoke` job 执行该脚本。
- CI `go` job 新增 `gofmt` 检查与 `go vet`，网关测试改以 `-race` 运行。
- [docs/operations/backup-restore.md](docs/operations/backup-restore.md) 新增容器部署中的备份与恢复（2.5 / 3.4 节）：镜像不含 `sqlite3` 命令行、根文件系统只读，改在容器内用备份 API 生成快照并写回；Kubernetes 上 PVC 应使用块存储。

### Changed

- README 策略引擎描述修正：`allow/deny/degraded` 三态改为 `allow/deny/require_approval` 三态（对齐 v1.0.1 已交付语义），并补充说明 Decision Router 的 `OK/FALLBACK_REQUIRED/FAILED` 为决策路由层状态、与 Policy 三态不混用。
- [docs/releases/v1.0.md](docs/releases/v1.0.md) 插入"勘误与现状（2026-10-11）"节：澄清 v1.0.0 发布记录中 PostgreSQL 相关表述与 ADR-013 不一致，当前权威存储为 SQLite、PostgreSQL 为目标态，文档中 PostgreSQL 备份/迁移命令不可用。
- 包元数据事实统一：全部 10 个子包 `requires-python` 从 `>=3.9` 校准为 `>=3.12`（对齐 README/CI/Ruff 的 Python 3.12 基线）；`control-plane` 与 `workflow-runtime` 包描述去除过时的 skeleton/placeholder 表述，`cli` 与 `node-agent` 包描述同步移除 skeleton 字样。
- [.github/workflows/ci.yml](.github/workflows/ci.yml)：ADR 检查由硬编码 001–012 改为索引驱动动态校验；architecture-docs job 新增 manifest / OpenAPI 语义 / 元数据一致 / 插件冒烟四道步骤。
- 修正 `plugins/runtime-tgi`（移除 schema 未定义的 spec.capabilities）与 `plugins/workflow-temporal`（category 改为 provider 并移除 capabilities）manifest 至 schema 合规。
- **破坏性**：网关与控制平面默认使用 `apikey` 认证模式，未配置 key 文件时拒绝启动；`dev` 模式（直接信任租户请求头）只允许监听回环地址。
- **破坏性**：缺少凭证的请求由 `400`（缺少 `X-Tenant-Id`）改为 `401 UNAUTHENTICATED`，并携带 `WWW-Authenticate: Bearer realm="forgemesh"`；`X-Tenant-Id` 与凭证绑定的租户不一致返回 `403 FORBIDDEN`；读接口要求 `read` scope，写接口（含启动工作流与请求决策）要求 `write` scope。
- **破坏性**：网关的 `401` 响应体由纯文本改为统一错误信封 `{"error": {"code", "message", "correlationId"}}`（新增的 `403` 同样使用信封）；推理实例不再收到调用方的 `Authorization` / `Cookie`。
- 控制平面全部错误响应统一为错误信封：框架层 404 / 405 不再返回 `{"detail": ...}`，请求校验失败由 `422` 改为 `400`，未捕获异常返回不含内部细节的 `500 INTERNAL_ERROR`（细节写入日志）。
- 网关接受任意大小写的 `Bearer` scheme（如 `bearer`）。
- **破坏性**：网关消费的 `status.endpoints` 由字符串数组改为对象数组（见 Added），只使用 `ready` 且协议已知的实例；JSON Schema 中资源模型从未实现的单数 `status.endpoint` 一并移除。
- **破坏性**：网关默认不连接回环、链路本地、未指定、组播地址与已知云元数据服务地址上的推理实例（见 Security）；实例运行在本机时需设置 `GATEWAY_INSTANCE_ALLOWED_CIDRS`（`make run-dev` 已允许 `127.0.0.0/8,::1/128`）。
- 网关连接池：流式代理与控制平面客户端每个主机最多保留 64 个空闲连接（此前为 `net/http` 默认的 2 个，一波并发请求之后大部分连接被关闭，下一波重新建连）；资源反代等待控制平面响应头最多 30 秒。
- 网关 Resolver 合并同一服务的并发未命中，只查询一次控制平面，查询不随发起它的请求取消；"服务不存在 / 未就绪 / 无可用实例"缓存 1 秒（不超过 `GATEWAY_RESOLVER_TTL_SECONDS`），`403`、其他控制平面错误与网络错误仍不缓存。
- 控制平面的 SQLite 存储（Registry、去重键、任务、审计、Outbox）统一以 WAL 模式打开并启用外键约束（`forgemesh.controlplane.sqlite.connect`）。此前为回滚日志模式，而 Registry 与去重键存储共用同一个文件：一个连接上的读事务未结束时，另一个连接的提交只能等待，超过 5 秒以 `database is locked` 失败。运行期间数据库文件旁会有 `-wal` / `-shm` 文件，不能直接复制文件做备份；`synchronous` 保持 `FULL`。
- 去重键存储 `ProcessedKeyStore` 不再在每次 `seen` / `mark` 时删除过期键并提交：`seen` 不报告超过 TTL 的键，过期键最多每 `min(TTL, 60 秒)` 删除一次，启动后的第一次调用即清理上次运行留下的过期键。
- `deploy/docker/docker-compose.yaml` 改为可运行的本地拓扑：control-plane 与 gateway 从本仓库的 Dockerfile 构建，共用 compose secret 中的 key 文件 `deploy/docker/api-keys.json`（需事先生成，已加入 `.gitignore`），只发布在 `127.0.0.1`，以只读根文件系统、去掉全部 capability 运行；gateway 在控制平面 `/readyz` 通过后才启动；控制平面数据库在卷 `control-plane-data` 中；postgres 移入 `postgres` profile，默认不启动；移除过时的 `version` 键。
- Kubernetes 控制平面清单：数据库放在 PVC `forgemesh-control-plane-data`（ReadWriteOnce）上，更新策略为 `Recreate`；key 文件来自 Secret `forgemesh-api-keys`（键名 `api-keys.json`，部署前需创建）；Pod 以 uid 10001、只读根文件系统运行，不挂载 ServiceAccount token。
- pytest 把 `RuntimeWarning` 与 `PytestUnraisableExceptionWarning` 视为错误：协程未被 await 时被调用的代码实际没有执行，测试现在会失败，而不是只打印一条警告。
- README 与 CONTRIBUTING 的安装命令改为 `make install`：在仓库根目录执行 `uv sync` 只会创建一个空的虚拟环境，随后 `make test` 找不到 pytest。README 目录树列出全部 10 个插件，并补充容器部署说明。
- [docs/architecture/module-mapping.md](docs/architecture/module-mapping.md)：更正网关文件与测试数量（33 / 17）和上游变量名（`GATEWAY_UPSTREAM_URL`），部署小节与 CI 小节按当前工件更新，偏差 D2 处置后移除。
- 删除源码与测试 docstring 中「兼容 Python 3.9+」/「兼容 3.9–3.12」的说法（各子包 `requires-python` 已是 `>=3.12`，代码使用 `X | None` 与 `enum.StrEnum`，在 3.9 上无法导入）；CONTRIBUTING 的类型注解约定改为与 ruff `UP` 规则一致的 `list[X]` / `X | None`。

### Deprecated

- `GET /api/v1/inference-services` 的 `tenantId` 查询参数：从未生效，租户取自凭证（ADR-014）；计划在 1.3.0 移除。

### Fixed

- 控制平面唯一的可运行应用仍注册阶段 0 占位路由，全部业务端点返回 `501`，已实现的 API 路由只在测试中挂载：现挂载真实路由；SQLite 在应用启动（lifespan）时打开，导入模块不再产生文件副作用；`/readyz` 探测权威存储可用性。
- `control-plane` 包补齐 `forgemesh-policy-engine` 与 `forgemesh-plugin-sdk` 依赖声明（此前独立安装后导入即失败）。
- `POST /api/v1/inference-services` 丢弃请求体中的 `spec`、接受任意 `kind`、不分配 `uid` 且返回 `200`：现按 `InferenceService` 强类型校验（`uid`、租户与 `status` 由服务端维护），成功返回 `201`，请求体不合法返回 `400` 错误信封，同名冲突返回 `409`。
- 列表接口的 `limit` 原样传给 SQLite，`limit=-1` 返回全部行：API 层限定为 `[1, 1000]`，越界返回 `400`；Registry 对 `limit < 1` 抛出 `ValueError`。
- Registry 标签选择器在 `LIMIT` 之后才在 Python 中过滤，首页之外的匹配项永远不会返回，翻页会静默跳过数据：现以 `json_extract` 在 SQL 中先过滤、再排序分页。
- Registry 全部方法在 `asyncio.to_thread` 工作线程上共用一个 sqlite3 连接且无锁，交错事务中失败写入者的回滚会丢弃其他写入者尚未提交的插入（创建报告成功但未持久化，R-1）：现以实例级 `RLock` 串行化全部数据库调用。
- `namespace` 为空（NULL）的资源可以同名重复创建（SQLite 主键视 NULL 互不相等），一次 DELETE 会删除全部副本：新增唯一表达式索引 `(tenant_id, kind, COALESCE(namespace, ''), name)`；打开已含重复数据的旧库时抛出 `PreconditionFailedError`，不静默丢行。
- `request_delete` 与 `release_allocation` 读后写不校验版本，期间提交的写入会丢失、期间追加的 finalizer 会被绕过：现以读取到的 `resource_version` / 状态为条件做 CAS，竞争失败抛出 `ConflictError`（DELETE 端点返回 `409`）。
- 调和器在 deny / require_approval / 分配失败等无副作用路径上也记录去重键，重新入队后命中幂等短路，被阻塞的资源在 spec 变更或 24h 去重 TTL 过期前不会被重新评估：这些路径不再记录去重键。
- 生产代码中没有任何地方驱动调和器，创建的资源永远停在 `Pending`、`status` 从不更新：现由控制器驱动（见 Added）。
- 调和器在没有注入调度器或工作流运行时的情况下跳过分配与部署、登记去重键并报告 `Ready=True`（`Reconciled`），实际什么都没做：现在任何副作用之前检查，返回 `Ready=False`，原因为 `SchedulerUnavailable` 或 `WorkflowRuntimeUnavailable`，且不登记去重键。目前仓库没有调度器实现，默认部署中新建的服务会停在 `Pending` 并带有 `Ready=False`（`SchedulerUnavailable`）。
- 网关把入站路径原样拼接到实例地址，请求以 `/api/v1/inference/{svc}/chat` 到达推理运行时，而 vLLM 与 TGI 都不提供这个路径：现按实例的 `protocol` 转发到 `/v1/chat/completions`（`openai-chat`）或 `/generate_stream`（`tgi-generate`），查询串保留。
- 网关解析推理服务时不带命名空间，控制平面因此只查找不带命名空间的服务，带命名空间的服务无法经网关调用：现由带命名空间的路由解析（见 Added）。
- OpenAPI 没有描述 `GET` / `DELETE /api/v1/inference-services/{name}` 的 `namespace` 查询参数，而实现一直支持它，网关解析带命名空间的服务时也依赖它：现已补上，并写明省略时指不带命名空间的资源（列表接口省略时则不按命名空间过滤）。
- 网关 `/readyz` 只对上游做 TCP 拨号，控制平面的权威存储不可用（其 `/readyz` 返回 `503`）或端口被其他进程占用时仍报告就绪：现请求控制平面的 `/readyz`，返回 `200` 才就绪，否则返回 `503`（`upstream not ready` / `upstream unreachable`）。
- 推理实例返回错误（如 vLLM 对非法参数返回 `400` JSON）时，网关把响应的 `Content-Type` 改成 `text/event-stream`：现只对 2xx 响应这样设置，错误响应保留实例的 `Content-Type`。
- [docs/operations/backup-restore.md](docs/operations/backup-restore.md) 的热备份方法「`PRAGMA wal_checkpoint(TRUNCATE)` 后 `cp` 主库文件」在控制平面写入期间不安全：复制途中的自动 checkpoint 会改写主库文件，备份可能不一致甚至损坏；脚本只看 `PRAGMA integrity_check` 的退出码，而 `sqlite3` 检查出损坏时同样返回 `0`。现改为 `VACUUM INTO`（脚本）/ `Connection.backup`（Python）在线快照，校验输出必须为 `ok`，数据库路径不存在时报错，而不是备份一个新建的空库。
- 备份文档列出的 `data/registry.db` / `audit.db` / `decisions.db` 并不存在，控制平面的权威状态只在 `FORGEMESH_DB_PATH` 一个文件中。恢复时旧库的 `-wal` / `-shm` 随旧库一起改名保留（`-wal` 中可能有尚未合并回主库的已提交事务），全量恢复示例改为先停止、再启动控制平面。
- compose 拓扑无法运行（module-mapping 偏差 D2）：引用的 `Dockerfile.control-plane` 不存在，gateway 没有构建配置；compose 设置的 `FORGEMESH_UPSTREAM` 网关并不读取（网关读取 `GATEWAY_UPSTREAM_URL`），端口写成 `8000`（网关监听 `8081`）；控制平面默认只监听容器内的回环地址，网关访问不到；数据库写在容器文件系统中，重建容器即丢失；控制平面依赖它并不使用的 postgres，postgres 以默认口令发布在所有网卡上。现改为见 Changed。
- Kubernetes 控制平面清单把数据库写在容器文件系统中，Pod 重建即丢失全部资源；默认的 `RollingUpdate` 在更新期间同时运行新旧两个 Pod，违反 ADR-013 的单写者约束；控制平面默认只监听回环地址，kubelet 探针访问不到。现改为 PVC + `Recreate`，并设置 `FORGEMESH_HOST=0.0.0.0`（见 Changed）。
- `tests/unit/controlplane/test_registry.py` 有两处 `reg.close()` 未被 await，关闭逻辑从未执行。

### Security

- Go 网关多租户安全验证：跨租户拒绝、伪造 `X-Tenant-Id` 不得提权、SSE 客户端取消 / 上游断开连接释放、中间件链序（Recovery→Correlation→Authenticate→RateLimit→LimitRequestBody→Route）测试；网关 README 新增 header 转发 / 剥离策略表。
- 租户身份改由凭证绑定（ADR-014）：此前网关只检查 `X-Tenant-ID` 是否存在，控制平面直接信任该请求头且可被直连，任何调用方都能读写任意租户的资源。控制平面现自行校验凭证，不依赖请求必经网关；网关对推理路由检查 `read` scope，命中 Resolver 缓存的请求同样受约束。
- 网关不再把调用方的 `Authorization` / `Cookie` 转发给推理实例（此前原样透传，凭证会泄露给实例）；资源反代与 Resolver 查询仍向控制平面转发 `Authorization`，由控制平面复核。
- 网关限流改按已验证的租户计数，不再读取客户端可控的租户头或 `X-Forwarded-For`（此前更换头值即可获得新的令牌桶）。
- 网关只连接允许网段内的推理实例：此前流式代理会连接 `status.endpoints` 中的任意地址，实例地址被写成 `169.254.169.254` 或内网服务时，网关就成了访问它们的跳板。现在拨号时逐个检查解析出的 IP（主机名之后解析到别处同样生效）；`GATEWAY_INSTANCE_ALLOWED_CIDRS` 列出允许的网段，未设置时拒绝回环、链路本地、未指定、组播地址与已知云元数据服务地址；被拒绝的连接返回 `502 INSTANCE_ADDRESS_NOT_ALLOWED`，并计一次实例失败。
- 网关请求体此前不设上限，原样转发给控制平面与推理实例：现限制为 `GATEWAY_MAX_REQUEST_BODY_BYTES`（见 Added）。

## [1.0.1] - 2026-10-11

### Added

- 新增 [ADR-013](docs/design-decisions/adr-013.md)：权威存储采用 SQLite 先行、PostgreSQL 为目标态的过渡决策。
- finalizer 受控删除闭环：`request_delete` → `Terminating` → `ReleaseProvider` → 清空 finalizer（默认 `forgemesh.io/resource-release`）→ 物理删除；失败重入保持 `Terminating`，全流程幂等。
- Allocation 持久化表：部分唯一索引 `idx_allocations_one_active_per_resource`（`ON allocations(resource_id) WHERE status = 'Active'`）保证同一资源至多一个 Active 分配。
- `POST /api/v1/workflows` 与 `POST /api/v1/decisions` 端点正式实现（工作流启动返回 202、结构化决策请求）。
- Go 网关推理服务路由 Resolver：`{svc}` → 实例 endpoint 解析（消费控制平面 `status.endpoints`）、TTL 缓存 5s、健康摘流（连续 3 次失败冷却 10s）、轮询负载均衡与 `X-Tenant-Id` 透传。
- `tests/performance` 性能基准层（4 项：API P99、协调吞吐、重启恢复、冲突正确性；网关流式首字节基准属 Go 网关 CI 范围）。
- 调和去重键 SQLite 持久化（`ProcessedKeyStore`）：控制平面重启后已处理事件不重复调和。

### Changed

- `PolicyDecisionType` 恢复 `allow` / `deny` / `require_approval` 三态：移除 `degraded` 取值，条件放行语义转入 obligations 表达。
- `DecisionRouter` 路由结果新增 `status` 三态（`OK` / `FALLBACK_REQUIRED` / `FAILED`）：高风险请求的候选来自备用 Provider 时返回 `FALLBACK_REQUIRED` 且不自动采用，交由调用方决定；全部失败返回 `FAILED`，绝不解释为"允许执行默认操作"。
- `DecisionRequest.risk_level` 恢复 `Literal["low", "medium", "high"]` 强类型（默认 `low`）、`deadline_ms` 恢复 `int` 强类型与默认值。
- 推理服务状态转移表开放"任意非吸收态（含 `Failed` / `Unknown`）→ `Terminating`"，删除入口对所有现实状态可达。
- `store.put` 接线状态机校验：非法状态转移在持久层被拒绝（`InvalidStateTransition`）。
- DELETE 端点对存在副作用（finalizer 非空）的资源返回 `202 Terminating`，不再直接物理删除。

### Fixed

- 修复决策路由风险等级字典序比较 bug：`"low" >= "high"` 在字符串比较下为 `True`，导致低风险请求被误判为高风险；改为显式等级序（rank）比较。
- 清理 OpenAPI 中已实现端点（workflows/decisions 等）的 `501 Not Implemented` 残留标记。
- 勘误：1.0.0 变更日志 P1 段中"PostgreSQL 权威存储（ADR-007）：Repository 模式、迁移脚本"的表述与实际不符——v1.0.0 实际交付为 SQLite 权威存储先行（`SQLiteRegistry` 实现 `ResourceRegistry` 抽象、CAS 乐观并发 `resource_version`），未引入 PostgreSQL 部署与迁移脚本。PostgreSQL 仍为权威存储目标态（ADR-007），过渡决策与迁移判据详见 [ADR-013](docs/design-decisions/adr-013.md)。

### Security

- 决策高风险降级保护：高风险请求的候选来自备用 Provider 时不再自动采用（`FALLBACK_REQUIRED`），防止降级路径绕过审批与授权语义（对齐 ADR-004 / ADR-005）。

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

[1.0.1]: https://github.com/Mekoforge/ForgeMesh/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Mekoforge/ForgeMesh/releases/tag/v1.0.0