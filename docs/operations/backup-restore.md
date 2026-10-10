# 备份与灾备演练

> 适用范围：ForgeMesh 控制平面 SQLite 权威状态存储（资源注册表 / 审计日志 / 决策记录）。
>
> 对齐 ADR-007（注册表为权威状态）、development-plan WP-4.7 安全测试与灾备。

## 1. 概述

ForgeMesh 控制平面使用 SQLite 作为权威状态存储，涉及以下数据库文件：

| 数据库 | 模块 | 默认路径 | 内容 |
|--------|------|----------|------|
| 资源注册表 | `forgemesh.controlplane.registry.SQLiteRegistry` | `data/registry.db` | 资源对象（spec/status/conditions） |
| 审计日志 | `forgemesh.controlplane.audit.AuditLog` | `data/audit.db` | 审计事件（correlation_id 贯计贯穿） |
| 决策记录 | `forgemesh.decision.records.DecisionRecordStore` | `data/decisions.db` | 决策记录与审批状态 |

### RPO / RTO 目标

| 指标 | 目标 | 说明 |
|------|------|------|
| **RPO**（恢复点目标） | ≤ 5 分钟 | 定时备份周期 5 分钟，WAL checkpoint 保证已提交事务持久化 |
| **RTO**（恢复时间目标） | ≤ 15 分钟 | 从检测到故障到服务恢复，含备份传输 + 文件恢复 + 进程重启 |
| **备份保留** | 7 天 × 24 份/天 | 每日 288 份增量，保留最近 168 份全量 |
| **演练频率** | 每月 1 次 | 灾备演练检查清单全量执行 |

---

## 2. SQLite 备份策略

### 2.1 WAL 模式与 Checkpoint

SQLite 启用 WAL（Write-Ahead Logging）模式后，写入先追加到 `-wal` 文件，
定期 checkpoint 合并回主库文件。备份前**必须**执行 checkpoint，确保
主库文件包含所有已提交事务。

```sql
-- 1. 强制将 WAL 中所有帧合并回主库（TRUNCATE 模式清空 -wal 文件）
PRAGMA wal_checkpoint(TRUNCATE);

-- 2. 确认 checkpoint 后的 WAL 文件大小为 0
PRAGMA journal_mode;  -- 应返回 'wal'
```

### 2.2 备份方法：文件复制 + WAL Checkpoint

采用「checkpoint + 文件级复制」方案，保证备份一致性且无需停服：

```bash
#!/usr/bin/env bash
# forgemesh-backup.sh — SQLite 数据库备份脚本
# 用法: ./forgemesh-backup.sh <db_path> <backup_dir>
set -euo pipefail

DB_PATH="${1:?usage: $0 <db_path> <backup_dir>}"
BACKUP_DIR="${2:?usage: $0 <db_path> <backup_dir>}"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
BACKUP_FILE="${BACKUP_DIR}/$(basename "${DB_PATH}" .db)-${TIMESTAMP}.db"

mkdir -p "${BACKUP_DIR}"

# Step 1: WAL checkpoint（将已提交事务合并回主库文件）
sqlite3 "${DB_PATH}" "PRAGMA wal_checkpoint(TRUNCATE);"

# Step 2: 文件级复制（主库文件已包含全部已提交数据）
cp "${DB_PATH}" "${BACKUP_FILE}"

# Step 3: 校验备份完整性
sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;" > /dev/null

# Step 4: 计算摘要（供恢复时校验）
sha256sum "${BACKUP_FILE}" > "${BACKUP_FILE}.sha256"

echo "backup completed: ${BACKUP_FILE}"
```

### 2.3 备份调度（cron）

```cron
# 每 5 分钟备份一次三个数据库
*/5 * * * * /opt/forgemesh/bin/forgemesh-backup.sh /var/lib/forgemesh/data/registry.db   /var/backups/forgemesh/registry
*/5 * * * * /opt/forgemesh/bin/forgemesh-backup.sh /var/lib/forgemesh/data/audit.db      /var/backups/forgemesh/audit
*/5 * * * * /opt/forgemesh/bin/forgemesh-backup.sh /var/lib/forgemesh/data/decisions.db  /var/backups/forgemesh/decisions

# 每日清理 7 天前的备份
0 3 * * * find /var/backups/forgemesh -name '*.db' -mtime +7 -delete && find /var/backups/forgemesh -name '*.sha256' -mtime +7 -delete
```

### 2.4 Python 备份接口（伪代码）

```python
"""ForgeMesh SQLite 备份工具。"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


async def backup_database(db_path: Path, backup_dir: Path) -> Path:
    """备份单个 SQLite 数据库：WAL checkpoint + 文件复制 + 完整性校验。

    Args:
        db_path: 源数据库文件路径。
        backup_dir: 备份目录。

    Returns:
        备份文件路径。
    """
    def _backup() -> Path:
        # Step 1: WAL checkpoint
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        # Step 2: 文件复制
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{db_path.stem}-{timestamp}.db"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_path, backup_path)

        # Step 3: 完整性校验
        verify_conn = sqlite3.connect(str(backup_path))
        result = verify_conn.execute("PRAGMA integrity_check").fetchone()
        verify_conn.close()
        if result[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {backup_path}")

        # Step 4: 摘要文件
        digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        (backup_path.with_suffix(".db.sha256")).write_text(f"{digest}  {backup_path.name}\n")

        return backup_path

    return await asyncio.to_thread(_backup)
```

---

## 3. 恢复流程

### 3.1 恢复步骤

| 步骤 | 操作 | 命令 / 说明 | 预计耗时 |
|------|------|-------------|----------|
| 1 | 停止控制平面进程 | `systemctl stop forgemesh-controlplane` | < 30s |
| 2 | 选择备份版本 | 从 `/var/backups/forgemesh/<db>/` 选择最近的有效备份 | < 1min |
| 3 | 校验备份摘要 | `sha256sum -c <backup>.db.sha256` | < 30s |
| 4 | 校验备份完整性 | `sqlite3 <backup>.db "PRAGMA integrity_check;"` | < 30s |
| 5 | 备份当前（损坏的）数据库 | `mv data/registry.db data/registry.db.broken` | < 10s |
| 6 | 恢复备份文件 | `cp <backup>.db data/registry.db` | < 1min |
| 7 | 删除残留 WAL/SHM | `rm -f data/registry.db-wal data/registry.db-shm` | < 10s |
| 8 | 启动控制平面进程 | `systemctl start forgemesh-controlplane` | < 2min |
| 9 | 验证服务健康 | `curl http://localhost:8080/healthz` 返回 200 | < 30s |
| 10 | 验证数据完整性 | 执行数据一致性检查脚本 | < 5min |

### 3.2 恢复脚本

```bash
#!/usr/bin/env bash
# forgemesh-restore.sh — SQLite 数据库恢复脚本
# 用法: ./forgemesh-restore.sh <backup_file> <target_db_path>
set -euo pipefail

BACKUP_FILE="${1:?usage: $0 <backup_file> <target_db_path>}"
TARGET_PATH="${2:?usage: $0 <backup_file> <target_db_path>}"

# Step 1: 校验摘要
sha256sum -c "${BACKUP_FILE}.sha256"

# Step 2: 校验完整性
INTEGRITY=$(sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;")
if [[ "${INTEGRITY}" != "ok" ]]; then
  echo "ERROR: backup integrity check failed: ${INTEGRITY}" >&2
  exit 1
fi

# Step 3: 备份当前数据库（若存在）
if [[ -f "${TARGET_PATH}" ]]; then
  mv "${TARGET_PATH}" "${TARGET_PATH}.broken.$(date -u +%s)"
fi

# Step 4: 恢复备份
cp "${BACKUP_FILE}" "${TARGET_PATH}"

# Step 5: 清除残留 WAL/SHM
rm -f "${TARGET_PATH}-wal" "${TARGET_PATH}-shm"

echo "restore completed: ${BACKUP_FILE} -> ${TARGET_PATH}"
```

### 3.3 全量恢复（三个数据库）

```bash
# 恢复全部三个数据库
./forgemesh-restore.sh /var/backups/forgemesh/registry/registry-20260101T120000Z.db  /var/lib/forgemesh/data/registry.db
./forgemesh-restore.sh /var/backups/forgemesh/audit/audit-20260101T120000Z.db       /var/lib/forgemesh/data/audit.db
./forgemesh-restore.sh /var/backups/forgemesh/decisions/decisions-20260101T120000Z.db /var/lib/forgemesh/data/decisions.db

# 重启服务
systemctl restart forgemesh-controlplane

# 验证健康
curl -sf http://localhost:8080/healthz | jq .
```

---

## 4. 灾备演练检查清单

> **演练频率**：每月 1 次。演练在隔离环境执行，不影响生产。

### 4.1 演练前准备

- [ ] 确认演练环境与生产环境配置一致（SQLite 版本、schema、WAL 模式）
- [ ] 备份当前演练环境数据（演练可回滚）
- [ ] 通知相关团队演练时间窗口

### 4.2 备份验证

- [ ] 执行 `forgemesh-backup.sh` 对三个数据库各生成一份备份
- [ ] 验证备份文件摘要：`sha256sum -c <backup>.db.sha256` 通过
- [ ] 验证备份完整性：`PRAGMA integrity_check` 返回 `ok`
- [ ] 验证备份可打开：`sqlite3 <backup>.db ".tables"` 列出预期表
- [ ] 验证备份数据行数与源库一致（抽样表）

### 4.3 恢复验证

- [ ] 停止控制平面进程
- [ ] 执行 `forgemesh-restore.sh` 恢复三个数据库
- [ ] 启动控制平面进程
- [ ] `/healthz` 探针返回 200
- [ ] `/readyz` 探针返回 200
- [ ] 资源注册表：`list` 返回预期资源数量
- [ ] 审计日志：`query_by_tenant` 返回预期记录数量
- [ ] 决策记录：`list_by_tenant` 返回预期记录数量

### 4.4 数据一致性验证

- [ ] **租户隔离**：跨租户 `get` 返回 `NotFoundError`（IVT-7）
- [ ] **审计可追溯**：`query_by_correlation` 返回完整调用链
- [ ] **correlation_id 贯计贯穿**：审计事件与决策记录的 correlation_id 一致
- [ ] **密钥脱敏**：日志中敏感字段均为 `"***"`，无明文泄露
- [ ] **资源版本连续**：`resourceVersion` 无跳跃（除已知 CAS 冲突）

### 4.5 RPO / RTO 验证

- [ ] **RPO**：恢复后最新数据与故障前时间差 ≤ 5 分钟
- [ ] **RTO**：从故障检测到服务恢复总耗时 ≤ 15 分钟
- [ ] 记录本次演练的实际 RPO / RTO 数值

### 4.6 演练后

- [ ] 清理演练环境临时文件
- [ ] 归档演练日志与结果记录
- [ ] 如有失败项，创建跟进 issue 并制定修复计划
- [ ] 更新演练记录文档（附实际 RPO / RTO 数据）

---

## 5. 故障场景与应对

| 场景 | 检测方式 | 应对措施 |
|------|----------|----------|
| 数据库文件损坏 | `PRAGMA integrity_check` 非 `ok` | 从最近有效备份恢复 |
| WAL 文件丢失 | 进程启动失败 / 数据缺失 | 从备份恢复（WAL 已 checkpoint 到主库） |
| 磁盘空间不足 | 监控告警 | 清理旧备份 + 扩容 |
| 备份文件损坏 | `sha256sum -c` 失败 | 回退到上一份有效备份 |
| 误删资源 | 用户报告 / 审计日志确认 | 从误删前备份恢复（需停服） |
| 租户数据串扰 | 安全测试失败 | 立即从备份恢复 + 排查隔离边界缺陷 |

---

## 6. 相关文档

- [架构设计](../architecture/architecture.md) — 权威状态存储与多租户隔离
- [开发计划](../architecture/development-plan.md) — WP-4.7 安全测试与灾备
- [ADR-007](../design-decisions/adr-007.md) — 注册表为权威状态
- [安全测试套件](../../tests/security/) — 租户隔离 / 插件权限 / 密钥脱敏