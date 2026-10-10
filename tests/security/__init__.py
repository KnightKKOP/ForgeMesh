"""ForgeMesh 安全测试套件。

覆盖：
* 租户隔离（IVT-7）：不同租户无法读取彼此私有资源与审计数据。
* 插件权限隔离：UNTRUSTED / TRUSTED / RESTRICTED_RPC 权限边界。
* 密钥处理：密钥引用不暴露明文、日志与诊断包自动脱敏。
"""
