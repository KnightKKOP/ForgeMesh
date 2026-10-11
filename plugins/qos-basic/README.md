# qos-basic

基础 QoS 策略插件：在准入/调度路径上评估配额与优先级，输出受控的策略裁决。

- **状态**：已实现（v1.0.0）
- **分类**：policy
- **契约**：`manifest.yaml`；实现须遵循 `packages/policy-engine` 契约与 `api/schemas/plugin-manifest.schema.json`。
- **注意**：配额变更竞争须由事务/唯一约束保护；策略评估只读，不直接写资源。
