# ForgeMesh

> **面向 AI 算力基础设施的开源自动化控制平台** —— 把 GPU、推理运行时、模型、节点与计算资源组织成可声明、可编排、可治理、可观测的 AI 服务。

ForgeMesh 不是 GPU 自动部署脚本的集合，也不是"仅靠 LLM 执行命令"的自动化 Agent，而是由**声明式资源模型、协调控制器、持久化工作流、类型化决策、策略治理、可插拔执行能力**共同组成的基础设施平台。

- 上游架构基线：[`docs/architecture/architecture.md`](docs/architecture/architecture.md)
- 开发计划（WBS / 里程碑 / 接口契约）：[`docs/architecture/development-plan.md`](docs/architecture/development-plan.md)
- 架构决策记录（ADR）：[`docs/design-decisions/`](docs/design-decisions/)
- 变更日志：[`CHANGELOG.md`](CHANGELOG.md)
- v1.0 发布记录：[`docs/releases/v1.0.md`](docs/releases/v1.0.md)

---

## 1. 架构六层概览

ForgeMesh 采用六个逻辑层次。这些是**逻辑边界**，不要求一开始就部署为六组独立服务。

| 层次     | 名称                       | 组成                                                                               | 职责                   |
| ------ | ------------------------ | -------------------------------------------------------------------------------- | -------------------- |
| **L1** | Access Layer             | REST API · CLI · Web Console · SDK                                               | 统一外部入口、身份认证与 API 兼容性 |
| **L2** | Core Control Plane（微内核）  | Resource Registry · Reconciliation · Policy · Scheduler · Plugin Manager · Audit | 权威状态、资源治理、权限与一致性     |
| **L3** | Orchestration & Decision | WorkEngine API · Decision Router · JEV API · Workflow Providers                  | 可靠执行与结构化决策           |
| **L4** | Plugin Capability Layer  | GPU · Runtime · Model · MIG · Quantization · QoS · Metering                      | 可替换的领域能力实现           |
| **L5** | Execution Plane          | Node Agent · Kubernetes · Container Runtime · GPU · Inference Services           | 实际资源操作与状态反馈          |
| **L6** | Platform Infrastructure  | SQLite · Outbox/Event Bus · Object Storage · Secret Manager · Observability      | 持久化、消息、制品、密钥与监控      |

### 四条不可违背的架构不变量

1. **核心掌握状态与治理权** —— 插件可以执行操作，但不能绕过核心直接改变其他模块的权威状态。
2. **目标状态与执行过程分离** —— 应当在什么状态由声明式资源与控制器决定；如何执行由 WorkEngine 负责。
3. **决策不等于授权** —— JEV 可以选择或推荐方案，但策略检查与资源可行性检查不能被跳过。
4. **关键状态必须能够恢复和追溯** —— 每次重要操作都必须具有稳定标识、版本与可查询的结果。

> 详细约束见 [`architecture.md` 第 2 章](docs/architecture/architecture.md)，开发落点见 [`development-plan.md` 第 1.2 节](docs/architecture/development-plan.md)。

---

## 2. 目录结构

```
forgemesh/
├── README.md                   # 本文件
├── LICENSE                     # Apache-2.0
├── CHANGELOG.md                # 变更日志（Keep a Changelog 格式）
├── CONTRIBUTING.md             # 贡献指南
├── CODE_OF_CONDUCT.md          # 行为准则
├── SECURITY.md                 # 安全策略与漏洞披露
├── GOVERNANCE.md               # 治理与决策机制
├── pyproject.toml              # 根级工具配置（ruff / pytest / coverage）
├── Makefile                    # install / test / lint / fmt / clean
├── requirements-dev.txt        # 开发依赖
│
├── api/                        # 对外契约（v1.0 稳定）
│   ├── schemas/                # JSON Schema（4 个核心模型）
│   ├── openapi/                # OpenAPI 规范（v1.0.0，x-stability: stable）
│   └── protobuf/               # Agent / gRPC 协议
│
├── packages/                   # Python 命名空间包（PEP 420，monorepo）
│   ├── core-contracts/         # 资源基类、Registry / Reconciler / Scheduler Protocol
│   ├── resource-model/         # InferenceService / Node / 分配 / 租户等强类型模型
│   ├── policy-engine/          # 授权、准入、配额、QoS
│   ├── decision-api/           # DecisionProvider / DecisionRouter / 熔断器 / 降级
│   ├── workflow-api/           # WorkflowRuntime 运行契约
│   ├── plugin-sdk/             # 插件 Manifest / 签名 / 隔离 / SDK / 脚手架
│   ├── control-plane/          # 模块化单体控制平面（Registry / Reconciler / API / Outbox / Audit / Observability / Diagnostics）
│   ├── workflow-runtime/       # 工作流执行引擎（持久化 / DSL / 补偿 / 审批 / GPU onboarding）
│   ├── node-agent/             # 节点侧受控执行组件（注册 / 探测 / 运行时 / 健康 / 恢复）
│   └── cli/                    # 命令行工具
│
├── gateway/                    # Go 高性能网关（REST / 流式代理 / 健康检查）
├── plugins/                    # 可插拔能力实现（10 个插件）
│   ├── decision-rules/         # 确定性规则决策 Provider
│   ├── decision-jev/           # JEV 类型化决策 Provider
│   ├── decision-typesafe-jev/  # TypeSafe AI Jev 决策 Provider
│   ├── workflow-reference/     # 参考工作流执行器
│   ├── workflow-temporal/      # Temporal 工作流 Provider
│   ├── gpu-nvidia/             # NVIDIA GPU Provider（发现 / 预检 / 健康 / MIG / 异构）
│   ├── runtime-vllm/           # vLLM 推理 Runtime Provider
│   ├── runtime-tgi/            # TGI 推理 Runtime Provider
│   ├── qos-basic/              # QoS 优先级与限流插件
│   └── metering-basic/         # Token 计量插件
├── deploy/                     # docker（Dockerfile / compose）/ kubernetes
├── tests/                      # unit / contract / compat / e2e / chaos / security
├── examples/                   # single-node / multi-node / multi-tenant 示例
├── scripts/                    # CI 校验脚本（manifest/OpenAPI/元数据/插件冒烟）
│
└── docs/
    ├── architecture/           # 架构基线与开发计划
    │   ├── architecture.md
    │   └── development-plan.md
    ├── design-decisions/       # ADR 索引与 ADR-001~012
    ├── governance/             # 弃用政策
    ├── operations/             # 备份与灾备
    ├── plugin-development/     # 插件开发指南
    └── releases/               # 发布记录（v1.0.md）
```

> 仓库结构的权威定义见 [`architecture.md` 第 17.1 节](docs/architecture/architecture.md)。

---

## 3. 快速开始

### 环境要求

- Python **3.12+**（CI 覆盖 3.12）
- GNU Make
- Go **1.22+**（仅当需要开发/构建 `gateway/` 时）
- Docker 与 Docker Compose v2（仅容器部署时）
- Git

### 安装与测试

```bash
# 1. 克隆仓库
git clone git@github.com:Mekoforge/ForgeMesh.git
cd ForgeMesh

# 2. 安装全部子包（editable）+ 开发依赖
make install

# 3. 运行全部测试
make test
```

常用命令：

```bash
make test-unit       # 仅单元测试
make test-contract   # 仅契约测试
make lint            # ruff 静态检查
make fmt             # ruff 自动修复与格式化
make clean           # 清理构建与缓存产物
```

> `make install` 会创建 `.venv`，遍历 `packages/*/`，对含 `pyproject.toml` 的子包执行 `pip install --no-deps -e`，因此新增子包无需修改顶层 Makefile。

### 运行控制平面

控制平面与网关默认要求 API key（[ADR-014](docs/design-decisions/adr-014.md)）：每个 key 绑定一个租户和一组 scope（`read` / `write` / `*`），租户只取自 key，不取自请求头。

```bash
# 1. 生成 key：第 2 行是 key（只显示这一次），最后一行是 key 文件条目
.venv/bin/python -m forgemesh.controlplane.keygen --tenant acme --subject ci-bot

# 2. 把条目放进 key 文件的 "keys" 数组（文件只含 SHA-256 摘要，网关与控制平面共用）
#    {"keys": [{"id": "acme-ci-bot", "sha256": "…", "tenantId": "acme", "subject": "ci-bot", "scopes": ["read", "write"]}]}

# 3. 启动并调用
FORGEMESH_API_KEYS_FILE=./api-keys.json FORGEMESH_DB_PATH=./forgemesh.db .venv/bin/forgemesh-control-plane
curl -s -H "Authorization: Bearer fmk_…" http://127.0.0.1:8080/api/v1/inference-services
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FORGEMESH_AUTH_MODE` | `apikey` | `apikey`：校验 `Authorization: Bearer <key>`；`dev`：直接信任 `X-Tenant-Id` 请求头，只允许监听回环地址，仅供本地开发 |
| `FORGEMESH_API_KEYS_FILE` | — | `apikey` 模式必填，缺失或格式非法时拒绝启动 |
| `FORGEMESH_DB_PATH` | `forgemesh.db` | SQLite 权威存储文件；同一文件只允许一个控制平面实例写入（ADR-013）。以 WAL 模式打开，运行时旁边有 `-wal` / `-shm` 文件，不能直接复制文件做备份，见 [备份与灾备演练](docs/operations/backup-restore.md) |
| `FORGEMESH_HOST` | `127.0.0.1` | 监听地址 |
| `FORGEMESH_PORT` | `8080` | 监听端口 |

> 请求可以不带 `X-Tenant-Id`；带了就必须与 key 绑定的租户一致，否则返回 `403`。缺少或无效的 key 返回 `401`。

控制平面启动后会自动协调 `InferenceService`：新建、修改或请求删除的资源在 0.5 秒内被处理，协调结果写入 `status.conditions`（例如 `Ready=False` 及其原因）。仓库目前还没有调度器实现，因此新建的服务会停在 `Pending`，并带有 `Ready=False`（`SchedulerUnavailable`）条件。控制器与 API 运行在同一个进程中，是权威存储的唯一写者（ADR-013）：请以单进程运行，不要使用 `uvicorn --workers` 或多副本。

### Go 网关

```bash
cd gateway
go test ./...       # 运行网关测试
go build ./cmd/gateway  # 构建网关二进制
GATEWAY_API_KEYS_FILE=../api-keys.json make run   # 默认 apikey 模式，监听 :8081
make run-dev        # dev 模式：信任租户请求头，只监听 127.0.0.1:8081
```

> 网关默认不连接回环、链路本地与云元数据地址上的推理实例。实例运行在本机时设置 `GATEWAY_INSTANCE_ALLOWED_CIDRS=127.0.0.0/8,::1/128`（`make run-dev` 已设置），见 [gateway/README.md](gateway/README.md)「实例地址限制」。

### 容器部署

```bash
# 1. 生成 key（同上），把 key 文件写到 deploy/docker/api-keys.json（已在 .gitignore 中），
#    权限设为 0644：两个容器都以非 root 用户读取它
# 2. 构建镜像并启动控制平面与网关（都只发布在 127.0.0.1）
docker compose -f deploy/docker/docker-compose.yaml up --build
curl -s -H "Authorization: Bearer fmk_…" http://127.0.0.1:8081/api/v1/inference-services

scripts/compose_smoke.sh   # 端到端冒烟（CI 同样执行）
```

控制平面的 SQLite 数据库在 compose 卷 `control-plane-data` 中。Kubernetes 清单见 `deploy/kubernetes/`：数据库放在 PersistentVolumeClaim 上，key 文件来自 Secret `forgemesh-api-keys`，更新策略为 `Recreate`，保证任何时刻只有一个控制平面写数据库（ADR-013）。容器中的备份与恢复见 [备份与灾备演练](docs/operations/backup-restore.md) 2.5 / 3.4 节。

---

## 4. 功能概览（v1.0.0）

ForgeMesh v1.0.0 是首个稳定版本，涵盖以下核心能力：

### 核心控制平面（M2）
- **资源注册中心**：SQLite 持久化，乐观并发 CAS，租户隔离，分页游标
- **协调控制器**：9 步声明式闭环（获取→查询→比较→生成候选→策略校验→调度分配→执行→重观察→更新状态）
- **策略引擎**：allow/deny/require_approval 三态决策，配额规则，高风险审批触发
  - 说明：Decision Router 的 `OK/FALLBACK_REQUIRED/FAILED` 是决策路由层的另一组状态，与 Policy 三态不混用
- **插件管理器**：Manifest 解析校验，生命周期状态机，能力发现，权限授予
- **审计与 Outbox**：事务性 Outbox，异步事件发布，幂等消费，correlation_id 全链路
- **REST API 与 CLI**：资源 CRUD，状态查询，任务管理，租户上下文注入

### 工作流与决策（M3）
- **工作流引擎**：持久化执行，幂等去重，三级超时，重试策略
- **参考执行器**：DSL 解析，补偿步骤（反向执行），审批等待与重校验
- **熔断器与降级**：closed/open/half-open 状态机，按风险决定是否尝试备用
- **决策插件**：decision-rules（确定性规则）、decision-jev（JEV 类型化决策）

### 执行平面与 GPU 生命周期（M4）
- **Node Agent**：注册认证，设备/驱动探测，本地运行时管理，断线重连，任务恢复
- **gRPC 任务协议**：6 类任务枚举，心跳与离线检测，幂等键去重
- **gpu-nvidia 插件**：GPU 枚举，能力采集，驱动/CUDA 预检，周期性健康检查
- **runtime-vllm 插件**：模型拉取，实例部署，冒烟验证，租约释放
- **GPU 上线 E2E**：发现→验证→准备→部署→验收→提交 完整闭环
- **故障恢复**：节点离线不误判、控制平面重启恢复、重复事件不盲目重试

### 生产级治理（M5）
- **配额与 QoS**：多租户配额策略，服务优先级排队，限流
- **Token 计量**：追加式 UsageRecord，计量修正事件
- **可观测性**：OpenTelemetry 接入，8 类关键指标，敏感数据过滤
- **诊断包**：统一诊断聚合（资源/工作流/插件/决策/日志），按租户权限受限
- **插件安全**：制品签名验证，隔离宿主（受限 RPC），升级兼容性
- **安全测试**：租户隔离，插件权限，密钥脱敏，灾备文档

### 扩展与生态（M6）
- **MIG 支持**：NVIDIA MIG 实例管理（创建/销毁/发现/健康检查/能力报告），并发安全，资源不超分
- **异构 GPU**：非 NVIDIA 能力差异显式报告（差异不隐藏）
- **Temporal Provider**：WorkflowRuntime 契约适配，补偿注册，信号投递
- **TGI Runtime**：HuggingFace TGI 推理 Runtime Provider（deploy→verify→release 闭环）
- **插件 SDK**：脚手架生成，Manifest 校验，打包发布，开放契约测试套件
- **治理文档**：GOVERNANCE / SECURITY / CONTRIBUTING / CODE_OF_CONDUCT 完善，弃用政策
- **稳定 API**：OpenAPI v1.0.0 标记，CHANGELOG，发布记录，CI 架构文档检查

### 测试统计

| 类型 | 数量 |
|------|------|
| Python 测试 | **1115** |
| Go 测试 | 全部通过 |
| ruff 检查 | All checks passed |

---

## 5. 插件体系

ForgeMesh 通过可插拔架构支持 6 种插件类型：

| 类型 | 已实现插件 | 说明 |
|------|-----------|------|
| decision | `decision-rules`, `decision-jev` | 决策 Provider（规则 / JEV） |
| workflow | `workflow-reference`, `workflow-temporal` | 工作流执行后端 |
| runtime | `runtime-vllm`, `runtime-tgi` | 推理运行时 Provider |
| gpu | `gpu-nvidia` | GPU Provider（含 MIG / 异构） |
| metering | `metering-basic` | Token 计量 |
| qos | `qos-basic` | QoS 优先级与限流 |

插件开发指南见 [`docs/plugin-development/guide.md`](docs/plugin-development/guide.md)。

---

## 6. 里程碑

| 里程碑 | 阶段 | 状态 | 描述 |
|--------|------|------|------|
| M0-M1 | P0 | ✅ 完成 | 工程骨架 + 领域契约冻结 |
| M2 | P1 | ✅ 完成 | 核心控制平面 |
| M3 | P2 | ✅ 完成 | 工作流与决策 |
| M4 | P3 | ✅ 完成 | 执行平面与 GPU 生命周期 |
| M5 | P4 | ✅ 完成 | 生产级治理 |
| M6 | P5 | ✅ 完成 | 扩展与生态（v1.0.0 发布） |
| v1.1 | — | ✅ 已交付 | 可信基线（阶段 A–E），见 [`v1.1-development-plan.md`](docs/architecture/v1.1-development-plan.md) |

> 里程碑定义与准入/准出条件（DoD）见 [`development-plan.md`](docs/architecture/development-plan.md)。

---

## 7. 参与贡献

请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 与 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)；安全漏洞请遵循 [`SECURITY.md`](SECURITY.md) 的**私有披露**流程；项目治理与决策机制见 [`GOVERNANCE.md`](GOVERNANCE.md)。重大架构变更需通过 **ADR** 提案，模板见 [`docs/design-decisions/README.md`](docs/design-decisions/README.md)。

---

## 8. 许可证

本项目采用 **Apache License 2.0** 许可，完整文本见 [`LICENSE`](LICENSE)。

```
Copyright 2026 Mekoforge

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```
