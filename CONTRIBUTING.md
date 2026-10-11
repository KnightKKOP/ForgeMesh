# 贡献指南（Contributing to ForgeMesh）

感谢你参与 ForgeMesh！本指南说明开发流程、分支与提交规范、测试要求、ADR 流程、贡献者许可（DCO）与 PR 检查项。

> 请同时阅读 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。参与本项目即表示你同意遵守该行为准则。

---

## 1. 开发环境

### 1.1 环境要求

- Python **3.12+**（CI 覆盖 3.12）
- **uv**（Python 包与虚拟环境管理器，替代 pip/venv，安装见 <https://docs.astral.sh/uv/>）
- GNU Make
- Go **1.22+**（仅开发 `gateway/` 时需要）
- Git

### 1.2 初始化

**方式一：直接 clone（有写权限的贡献者）**

```bash
git clone git@github.com:Mekoforge/ForgeMesh.git
cd ForgeMesh
uv sync               # 创建虚拟环境并安装全部子包（editable）+ 开发依赖
make test             # 运行全部测试，确认基线通过
```

**方式二：fork → clone（外部贡献者推荐）**

```bash
# 1. 在 GitHub 上 fork 仓库到你自己的账号下
# 2. clone 你的 fork
git clone git@github.com:<你的用户名>/ForgeMesh.git
cd ForgeMesh
# 3. 添加上游远程，以便同步最新 main
git remote add upstream git@github.com:Mekoforge/ForgeMesh.git
git fetch upstream
# 4. 安装依赖
uv sync
make test
```

> 后续同步上游：`git fetch upstream && git checkout main && git merge upstream/main && git push origin main`

### 1.3 常用命令

| 命令 | 说明 |
| --- | --- |
| `uv sync` | 创建虚拟环境，安装全部子包（editable）+ 开发依赖 |
| `make install` | 等价于 `uv sync`（Makefile 封装） |
| `make test` | 运行全部测试（pytest） |
| `make test-unit` | 仅单元测试 |
| `make test-contract` | 仅契约测试 |
| `make lint` | ruff 静态检查 |
| `make fmt` | ruff 自动修复与格式化 |
| `make clean` | 清理构建与缓存产物 |

---

## 2. 开发流程

1. **认领或提交 Issue**：说明背景、期望行为与验收标准；重大变更先讨论方向。
2. **同步上游**：`git fetch upstream && git checkout main && git merge upstream/main`（fork 模式）或 `git pull origin main`（直接 clone 模式）。
3. **创建分支**：从最新 `main` 切出（见第 3 节命名规范）。
4. **编写代码与测试**：遵循第 5 节测试要求与项目代码规范。
5. **本地自检**：`make fmt && make lint && make test` 全绿。
6. **提交**：遵循第 4 节提交规范，使用 `-s` 签署 DCO（见第 6 节）。
7. **发起 PR**：从你的 fork 向 `Mekoforge/ForgeMesh` 的 `main` 发起 PR；填写 PR 模板，关联 Issue，逐项确认第 8 节 PR 检查项。
8. **评审与合并**：至少 1 名维护者/模块 Owner 批准；公共接口变更需按第 7 节完成兼容性评审。

---

## 3. 分支规范

| 类型 | 命名示例 | 用途 |
| --- | --- | --- |
| 功能 | `feat/resource-registry` | 新功能 |
| 修复 | `fix/state-transition-guard` | Bug 修复 |
| 文档 | `docs/adr-013-xxx` | 文档/ADR |
| 重构 | `refactor/decision-router` | 无行为变更的重构 |
| 构建/CI | `chore/ci-python-matrix` | 构建、CI、依赖 |

- 分支名小写、以 `-` 分隔。
- 一个分支聚焦一件事，避免混合无关改动。

---

## 4. 提交规范（Conventional Commits）

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

- **type**：`feat` / `fix` / `docs` / `refactor` / `test` / `build` / `chore` / `perf` / `ci`。
- **scope**：受影响的模块或子包，如 `core`、`resource`、`decision`、`gateway`、`ci`。
- **subject**：祈使句、简洁、不超过 72 字符。
- **破坏性变更**：在类型后加 `!`，并在 footer 写明 `BREAKING CHANGE:` 及迁移说明。

示例：

```
feat(resource): 为 InferenceService 状态机增加转移校验

- 定义 Pending→Scheduling→...→Ready 合法转移
- 非法转移抛出 InvalidStateTransition

Refs: #42
Signed-off-by: Zhang San <zhangsan@example.com>
```

---

## 5. 测试要求

- **每个 PR 必须包含与改动匹配的测试**，并在本地确保 `make test` 全绿。
- 测试分层（标记见根 `pyproject.toml`）：
  - `unit`：纯逻辑（状态机、策略、调度约束、类型转换）——每次 PR 必跑。
  - `contract`：插件 API、Provider 协议、Schema 兼容性——每次 PR 必跑。
  - `integration` / `e2e` / `chaos` / `security`：由 CI 分阶段执行。
- **新增公共契约（接口/Schema/事件）必须附带契约测试**，确保版本兼容。
- 测试应可重复、无外部副作用、不依赖真实 GPU/数据库（单元/契约测试）。
- 保持覆盖率不下降；核心模块（`packages/core-contracts`、`packages/resource-model`）优先。

> 测试布局见 `tests/unit`、`tests/contract`、`tests/integration`、`tests/e2e`、`tests/chaos`、`tests/security`。

### 5.1 文档要求

- **公共 API 必须有 docstring**：所有公共类、函数、方法、Protocol、数据模型须附带 docstring，说明用途、参数、返回值与异常。
- **docstring 语言**：中文描述 + 英文标识符；类型注解使用 `Optional` / `List` / `Dict`（而非 PEP 604 `X | None`），与项目风格一致。
- **复杂逻辑注释**：非显而易见的算法、状态机转移、策略选择等须注释说明"为什么"（why），而非"做什么"（what）。
- **文档同步**：新增或变更公共 API 时，同步更新相关文档（README / docs / OpenAPI / CHANGELOG）。
- **ADR 同步**：涉及架构决策的变更须同步创建或更新 ADR（见第 7 节）。
- **示例代码**：公共 API 的 docstring 中可附 usage 示例（doctest 风格），鼓励但非强制。

---

## 6. 贡献者许可（DCO）

本项目采用 **Developer Certificate of Origin (DCO)**。提交时请在 commit message 中添加签名，表示你确认有权提交该贡献并同意按本项目许可证发布：

```bash
git commit -s -m "feat(core): add ResourceRegistry protocol"
# 生成：Signed-off-by: Your Name <your@email.com>
```

- 使用与 Git 配置一致的真实姓名和邮箱。
- 对本仓库的贡献均以 **Apache-2.0** 许可证授权。
- 若贡献涉及第三方代码，必须注明来源与兼容许可证。

DCO 全文：<https://developercertificate.org/>

---

## 7. 架构决策记录（ADR）流程

重大架构决策（新增/变更公共接口、数据一致性语义、安全边界、插件协议等）必须通过 **ADR** 记录：

1. **提案**：复制 [`docs/design-decisions/README.md`](docs/design-decisions/README.md) 中的模板，新建 `docs/design-decisions/adr-0XX.md`。
2. **状态**：初始为 `Proposed`；经讨论后可为 `Accepted` / `Rejected` / `Superseded`。
3. **评审**：至少 1 名维护者 + 相关模块 Owner 评审；影响公共兼容性的须按第 7 节做兼容性评审。
4. **合并**：Accepted 的 ADR 与实现代码可同 PR 或先行合并；ADR 一旦 Accepted 即成为架构约束。
5. **修订**：不得直接改写已 Accepted 的 ADR 结论；应以新 ADR 标记 `Superseded by adr-0XX` 并说明差异。

ADR 与上游架构基线冲突时，以 [`architecture.md`](docs/architecture/architecture.md) 为准；确需变更基线的，走 ADR 修订。

---

## 8. PR 检查项

提交 PR 前请逐项确认：

- [ ] 分支基于最新 `main`，改动聚焦单一主题。
- [ ] 提交信息符合 Conventional Commits，并已 `Signed-off-by`（DCO）。
- [ ] `make fmt` 与 `make lint` 通过，无新增告警。
- [ ] `make test` 通过；新增/修改逻辑有对应测试。
- [ ] 公共接口/Schema/事件变更已附 **契约测试**，并评估兼容性与弃用计划。
- [ ] 重大架构决策已附 **ADR**（或引用既有 ADR）。
- [ ] 文档已同步更新（README / docs / 注释）。
- [ ] 无遗留调试代码、密钥、临时文件或大体积二进制。
- [ ] 安全相关改动已考虑租户隔离、密钥不落盘与最小权限（见 [`SECURITY.md`](SECURITY.md)）。
- [ ] 已关联相关 Issue（`Refs: #id` / `Closes: #id`）。

---

## 9. 代码风格

- **Python**：ruff 配置见根 `pyproject.toml`（行宽 100，`py312` 目标）。统一类型注解，使用 `Optional[X]` / `List[X]` / `Dict[K, V]`（而非 PEP 604 `X | None`）；公共 API 使用 Pydantic 强类型模型，禁止以未校验的任意 dict 作为公共接口。
- **Go**：`gateway/` 内遵循 `gofmt` / `go vet`；对外契约版本化。
- **通用**：命名清晰、函数单一职责、为复杂逻辑写注释说明"为什么"，不留 TODO 占位（除非关联 Issue）。
- 仓库文本统一使用 UTF-8，换行与缩进遵循 `.editorconfig`。