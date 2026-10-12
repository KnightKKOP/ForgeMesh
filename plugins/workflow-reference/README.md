# workflow-reference

参考工作流运行时：在后台按定义顺序执行工作流步骤，每一步的状态与结果都落盘；某一步用尽重试后反向补偿已完成的步骤，运行以 `Failed` 结束；进程重启后恢复未结束的运行。执行引擎是 `forgemesh.workflowruntime.runtime.LocalWorkflowRuntime`，本插件以子类 `ReferenceWorkflowRuntime` 提供，供插件管理器按清单加载；控制平面不导入插件，直接使用该引擎。

- **状态**：已实现（v1.0.0；1.1.0 起在后台执行运行）
- **分类**：provider
- **契约**：`manifest.yaml`；实现须遵循 `packages/workflow-api` 冻结的 WorkflowRuntime 契约（含 `get_run` / `find_run`）与 `api/schemas/plugin-manifest.schema.json`。
- **使用**：进程启动时调用 `recover()`，关闭时先 `aclose()` 再关闭 `WorkflowStore`。步骤 handler 拿不到运行 id：重启后，中断时正在执行的步骤以相同的输入再执行一次，幂等所需的标识须放在运行输入中。
- **注意**：`commit` 必须重校验资源版本/分配所有权/验收条件；高风险步骤支持人工审批与补偿。
