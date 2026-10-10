# decision-rules

基于规则（rule-based）的结构化决策 Provider：按声明式规则对资源放置/降级请求给出可解释决策。

- **状态**：占位 / 未实现（阶段 0）
- **分类**：provider
- **契约**：`manifest.yaml`；实现须遵循 `docs/architecture/development-plan.md`（5.5 决策）与 `api/schemas/plugin-manifest.schema.json`。
- **注意**：决策 ≠ 授权；本插件只产出决策与理由，最终分配授权仍由 Scheduler/Policy 完成。
