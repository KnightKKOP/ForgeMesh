"""ForgeMesh 混沌测试包。

验证系统在故障场景下的不变量保持（INV-2/INV-4/INV-5）：

* ``test_node_offline`` —— 节点离线后资源不误判可用；
* ``test_cp_restart`` —— 控制平面重启后工作流可恢复；
* ``test_duplicate_events`` —— 重复事件/结果丢失时不盲目重试。

所有测试使用 mock / 内存存储模拟故障，不需要真实硬件。
"""
