# decision-jev

JEV（Judgment–Evaluation–Verdict）结构化决策 Provider：以“判断-评估-裁决”链产出可审计决策与降级路径。

- **状态**：占位 / 未实现（阶段 0）
- **分类**：provider
- **契约**：`manifest.yaml`；实现须遵循 `docs/architecture/development-plan.md`（5.5）与 `api/schemas/plugin-manifest.schema.json`。
- **注意**：决策结果须可查询（审计），Provider 不可用时由 DecisionRouter 走降级路径。
