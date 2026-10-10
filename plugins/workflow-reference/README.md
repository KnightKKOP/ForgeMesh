# workflow-reference

参考工作流运行时：执行有序、可补偿的工作流步骤（plan/authorize/prepare/deploy/verify/commit）。

- **状态**：占位 / 未实现（阶段 0）
- **分类**：provider
- **契约**：`manifest.yaml`；实现须遵循 `packages/workflow-api` 冻结的 WorkflowRuntime 契约与 `api/schemas/plugin-manifest.schema.json`。
- **注意**：`commit` 必须重校验资源版本/分配所有权/验收条件；高风险步骤支持人工审批与补偿。
