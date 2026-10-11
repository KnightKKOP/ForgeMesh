# runtime-vllm

vLLM 推理运行时 Provider：准备并部署版本固定的 vLLM 推理服务，提供健康检查与检视。

- **状态**：已实现（v1.0.0）
- **分类**：runtime
- **契约**：`manifest.yaml`（含 `examples/single-node/plugin-vllm.yaml` 示例）；实现须遵循 `api/schemas/plugin-manifest.schema.json`。
- **注意**：运行时版本固定（ADR-011）；模型与制品以引用 + 摘要方式处理，不内嵌明文。
