# metering-basic

基础计量插件：消费事件流统计 Token/资源用量，并导出指标（默认只读）。

- **状态**：占位 / 未实现（阶段 0）
- **分类**：observability
- **契约**：`manifest.yaml`；实现须遵循 `api/schemas/plugin-manifest.schema.json` 与 `api/schemas/event.schema.json`。
- **注意**：计量消费者须按 eventId 幂等去重，且不得落盘敏感内容。
