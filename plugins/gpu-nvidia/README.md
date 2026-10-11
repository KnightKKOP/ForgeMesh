# gpu-nvidia

NVIDIA GPU 硬件 Provider：探测设备/驱动/MIG 与拓扑能力，并执行受控的设备分区。

- **状态**：已实现（v1.0.0）
- **分类**：hardware
- **契约**：`manifest.yaml`；字段须满足 `api/schemas/device-profile.schema.json`。
- **注意**：声明能力 ≠ 实际具备；能力以探测结果为准（ADR-010 / architecture 9.2），设备操作按最小权限授予。
