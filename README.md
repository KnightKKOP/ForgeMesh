# ForgeMesh

> **面向 AI 算力基础设施的开源自动化控制平台** —— 把 GPU、推理运行时、模型、节点与计算资源组织成可声明、可编排、可治理、可观测的 AI 服务。

ForgeMesh 不是 GPU 自动部署脚本的集合，也不是"仅靠 LLM 执行命令"的自动化 Agent，而是由**声明式资源模型、协调控制器、持久化工作流、类型化决策、策略治理、可插拔执行能力**共同组成的基础设施平台。

- 上游架构基线：[`docs/architecture/architecture.md`](docs/architecture/architecture.md)
- 开发计划（WBS / 里程碑 / 接口契约）：[`docs/architecture/development-plan.md`](docs/architecture/development-plan.md)
- 架构决策记录（ADR）：[`docs/design-decisions/`](docs/design-decisions/)

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
| **L6** | Platform Infrastructure  | PostgreSQL · Outbox/Event Bus · Object Storage · Secret Manager · Observability  | 持久化、消息、制品、密钥与监控      |

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
├── CONTRIBUTING.md             # 贡献指南
├── CODE_OF_CONDUCT.md          # 行为准则
├── SECURITY.md                 # 安全策略与漏洞披露
├── GOVERNANCE.md               # 治理与决策机制
├── pyproject.toml              # 根级工具配置（ruff / pytest / coverage）
├── Makefile                    # install / test / lint / fmt / clean
│
├── docs/
│   ├── architecture/           # 架构基线与开发计划
│   │   ├── architecture.md
│   │   └── development-plan.md
│   ├── design-decisions/       # ADR 索引与 ADR-001~012
│   └── ...
│
├── api/                        # 对外契约
│   ├── schemas/                # JSON Schema
│   ├── openapi/                # OpenAPI 规范
│   └── protobuf/               # Agent / gRPC 协议
│
├── packages/                   # Python 命名空间包（PEP 420，monorepo）
│   ├── core-contracts/         # 资源基类、Registry / Reconciler / Scheduler Protocol
│   ├── resource-model/         # InferenceService / Node / 分配 / 租户等强类型模型
│   ├── policy-engine/          # 授权、准入、配额规则
│   ├── decision-api/           # DecisionProvider 与 DecisionRouter
│   ├── workflow-api/           # WorkflowRuntime 运行契约
│   ├── plugin-sdk/             # 插件 Manifest 与隔离契约
│   ├── control-plane/          # 模块化单体控制平面
│   ├── workflow-runtime/       # 参考工作流执行器
│   ├── node-agent/             # 节点侧受控执行组件
│   └── cli/                    # 命令行工具
│
├── gateway/                    # Go 高性能网关（REST / 流式代理）
├── plugins/                    # 可插拔能力实现（decision-rules / gpu-nvidia / runtime-vllm ...）
├── deploy/                     # docker / kubernetes / examples
├── tests/                      # unit / contract / integration / e2e / chaos / security
└── examples/                   # single-node / multi-node / multi-tenant 示例
```

> 仓库结构的权威定义见 [`architecture.md` 第 17.1 节](docs/architecture/architecture.md)。

---

## 3. 快速开始

### 环境要求

- Python **3.9+**（CI 覆盖 3.9 与 3.11）
- GNU Make
- Go **1.22+**（仅当需要开发/构建 `gateway/` 时）

### 安装与测试

```bash
# 1. 创建虚拟环境
python3 -m venv .venv

# 2. 安装全部子包（editable）+ 开发依赖（会自动激活/使用 .venv）
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

> `make install` 会遍历 `packages/*/`，对含 `pyproject.toml` 的子包执行 `pip install -e`，因此新增子包无需修改顶层 Makefile。

---

## 4. 阶段 0 范围（当前里程碑 M1）

阶段 0 = **架构与领域契约（P0 → M1）**：目标是**领域契约与公共接口冻结**。

**本阶段交付（In Scope）**

- 领域契约与资源模型的**强类型定义**（`Resource` / `Metadata` / `Condition` 基类）。
- 资源模型：`InferenceService`、`Node`、`Accelerator`、`DeviceInstance`、`DeviceProfile`、`Allocation`、`ResourceLease`、`Tenant`、`Principal`。
- 状态机与**转移校验**（非法状态转移被拒绝）。
- 核心 Protocol：`ResourceRegistry`、`Reconciler`、`Scheduler`、`WorkflowRuntime`、`DecisionProvider` / `DecisionRouter`、`PolicyEngine`、`PluginManifest`。
- 公共 **Schema**：JSON Schema、OpenAPI、Protobuf 草案。
- 工程骨架：Python 包结构、Go 网关骨架、插件/示例/部署占位、契约测试框架。

**本阶段明确不做（Out of Scope）**

- ❌ **不包含真实 GPU 执行**——不实际驱动 NVIDIA/MIG 设备，GPU 能力以 Provider 契约与占位插件表达。
- ❌ **不包含真实数据库执行**——PostgreSQL DDL 仅为草案，持久化与事务性 Outbox 在后续阶段落地。
- ❌ 不实现完整工作流引擎、不接入真实 vLLM 运行时、不部署真实推理服务。
- ❌ 不实现完整多租户运行时、计量与生产级可观测。

> 阶段划分、准入/准出条件（DoD）与后续里程碑 M2–M6 见 [`development-plan.md` 第 1.3 / 第 3 章](docs/architecture/development-plan.md)。

---

## 5. 参与贡献

请先阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 与 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)；安全漏洞请遵循 [`SECURITY.md`](SECURITY.md) 的**私有披露**流程；项目治理与决策机制见 [`GOVERNANCE.md`](GOVERNANCE.md)。重大架构变更需通过 **ADR** 提案，模板见 [`docs/design-decisions/README.md`](docs/design-decisions/README.md)。

---

## 6. 许可证

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
