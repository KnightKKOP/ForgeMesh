# decision-typesafe-jev

TypeSafe AI 的 Jev（System One 结构化决策模型）决策 Provider：把决策上下文映射为 Jev 的 `state`、把候选动作映射为类型化 `questions`，再将 Jev 返回的「类型化、校准概率」决策映射为候选。

- **状态**：已实现（v0.1.0）
- **分类**：provider
- **契约**：`manifest.yaml`；实现须遵循 `docs/architecture/development-plan.md`（5.5 决策）、`docs/design-decisions/adr-004.md`（Jev 不是唯一决策系统）与 `api/schemas/plugin-manifest.schema.json`。
- **外部依赖**：TypeSafe 云端 API（https://docs.typesafe.ai/ ）。HTTP 层经 `JevClient` 协议注入，默认实现 `HttpJevClient` 仅用标准库；测试注入离线 `StaticJevClient`，无需真实网络。
- **注意**：**决策 ≠ 授权**（INV-3 / ADR-005）。Jev 输出仅为候选，`confidence`（校准概率）**绝不**作为授权凭证；必须经 Policy→Scheduler，且 Provider 超时/不可用时由 `DecisionRouter` 走降级路径，**绝不**默认放行。