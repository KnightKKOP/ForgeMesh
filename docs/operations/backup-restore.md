# 备份与灾备演练

> 适用范围：ForgeMesh 控制平面 SQLite 权威状态存储（`FORGEMESH_DB_PATH`）。
>
> 对齐 ADR-007（注册表为权威状态）、development-plan WP-4.7 安全测试与灾备。

## 1. 概述

ForgeMesh 控制平面（`forgemesh-control-plane`）把权威状态存放在**一个** SQLite 文件中，路径取自 `FORGEMESH_DB_PATH`（默认为工作目录下的 `forgemesh.db`）：

| 表 | 模块 | 内容 |
|----|------|------|
| `resources` / `allocations` / `resource_changes` | `forgemesh.controlplane.registry.SQLiteRegistry` | 资源对象（spec/status/conditions）、资源分配记录、控制器读取的变更记录 |
| `processed_dedup_keys` | `forgemesh.controlplane.reconciler.dedup.ProcessedKeyStore` | 控制器已处理的去重键 |

审计日志（`forgemesh.controlplane.audit.AuditLog`）、决策记录（`forgemesh.decision.records.DecisionRecordStore`）等其他 SQLite 存储默认使用内存库，控制平面进程不会打开它们；嵌入方把它们指向文件时，按本文同样的方法逐个备份。

### RPO / RTO 目标

| 指标 | 目标 | 说明 |
|------|------|------|
| **RPO**（恢复点目标） | ≤ 5 分钟 | 定时备份周期 5 分钟，每份备份是备份开始时刻的一致快照 |
| **RTO**（恢复时间目标） | ≤ 15 分钟 | 从检测到故障到服务恢复，含备份传输 + 文件恢复 + 进程重启 |
| **备份保留** | 7 天 | 每 5 分钟一份全量快照（每天 288 份），每日清理 7 天前的备份 |
| **演练频率** | 每月 1 次 | 灾备演练检查清单全量执行 |

### 单实例写入约束

依据 [ADR-013](../design-decisions/adr-013.md)，v1.0.x 权威存储为 SQLite，部署形态为**单控制平面实例**，以下约束为硬性要求：

- **不得启动多个并发写同一 SQLite 权威库的控制平面实例**：SQLite 单文件写锁与 WAL 模式均以单写者为前提，多实例并发写同一库会导致锁竞争（`database is locked`）甚至数据损坏。
- 部署编排中 control-plane 服务**不得扩展 replicas**（见 `deploy/docker/docker-compose.yaml` 顶部注释）；多实例部署（高可用/水平扩展）须先满足 ADR-013 的 PostgreSQL 迁移判据并完成迁移。
- **升级流程**（与本文备份/恢复流程配套）：**停写**（停止控制平面进程）→ **备份**（见 2.2 节）→ **升级**（替换二进制/镜像并应用变更）→ **恢复**（重启进程并验证 `/healthz` 与数据一致性）。

---

## 2. SQLite 备份策略

### 2.1 WAL 模式

控制平面以 WAL（Write-Ahead Logging）模式打开数据库（`forgemesh.controlplane.sqlite.connect`）：写入先追加到 `-wal` 文件，SQLite 再自动 checkpoint 合并回主库文件（`-wal` 每增长约 1000 页一次，最后一个连接关闭时再合并一次）。因此：

- 运行期间已提交的事务可能只在 `-wal` 里。**不要用 `cp` / `rsync` 复制运行中的数据库文件**：复制出的主库缺少这部分事务，复制途中发生的自动 checkpoint 还会改写主库文件，备份可能前后不一致甚至损坏。先执行 `PRAGMA wal_checkpoint` 再复制同样不安全：checkpoint 与复制之间、复制过程中控制平面仍在提交。
- 主库文件与 `-wal` / `-shm` 是一个整体，移动、删除、恢复时一起处理（见第 3 节）。
- 数据库放在本地磁盘上：WAL 依赖共享内存文件 `-shm`，不支持 NFS / SMB 等网络文件系统。

确认数据库处于 WAL 模式：

```bash
sqlite3 /var/lib/forgemesh/data/forgemesh.db "PRAGMA journal_mode;"  # 应返回 wal
```

### 2.2 备份方法：在线快照

`VACUUM INTO` 在一个读事务内把数据库写成新文件，得到备份开始时刻的一致快照，包含 `-wal` 中已提交的事务；WAL 模式下读不阻塞写，备份期间控制平面照常提交，无需停服。生成的文件不依赖 `-wal`，可以直接复制、归档。

> 不用 sqlite3 命令行的 `.backup`：它每次只复制 100 页，期间其他进程一提交就从头再来，控制平面持续写入时可能一直完不成。

```bash
#!/usr/bin/env bash
# forgemesh-backup.sh — SQLite 数据库在线备份脚本（控制平面无需停服）
# 用法: ./forgemesh-backup.sh <db_path> <backup_dir>
set -euo pipefail

DB_PATH="${1:?usage: $0 <db_path> <backup_dir>}"
BACKUP_DIR="${2:?usage: $0 <db_path> <backup_dir>}"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")
BACKUP_FILE="${BACKUP_DIR}/$(basename "${DB_PATH}" .db)-${TIMESTAMP}.db"

# 路径写错时 sqlite3 会新建一个空库，并"成功"备份这个空库
if [[ ! -f "${DB_PATH}" ]]; then
  echo "ERROR: database not found: ${DB_PATH}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

# Step 1: 在线快照
sqlite3 "${DB_PATH}" "VACUUM INTO '${BACKUP_FILE}';"
# VACUUM INTO 不对生成的文件执行 fsync
sync "${BACKUP_FILE}"

# Step 2: 校验备份完整性（检查出损坏时 sqlite3 的退出码仍为 0，必须比较输出）
INTEGRITY=$(sqlite3 "${BACKUP_FILE}" "PRAGMA integrity_check;")
if [[ "${INTEGRITY}" != "ok" ]]; then
  echo "ERROR: backup integrity check failed: ${INTEGRITY}" >&2
  exit 1
fi

# Step 3: 计算摘要（供恢复时校验）
sha256sum "${BACKUP_FILE}" > "${BACKUP_FILE}.sha256"

echo "backup completed: ${BACKUP_FILE}"
```

### 2.3 备份调度（cron）

```cron
# 每 5 分钟备份一次控制平面数据库（路径与 FORGEMESH_DB_PATH 一致）
*/5 * * * * /opt/forgemesh/bin/forgemesh-backup.sh /var/lib/forgemesh/data/forgemesh.db /var/backups/forgemesh

# 每日清理 7 天前的备份
0 3 * * * find /var/backups/forgemesh -name '*.db' -mtime +7 -delete && find /var/backups/forgemesh -name '*.sha256' -mtime +7 -delete
```

### 2.4 Python 备份接口（伪代码）

使用 SQLite 在线备份 API（`sqlite3.Connection.backup`）。`pages` 取默认值 `-1` 时在一个读事务内复制全部页，同样得到一致快照且不阻塞写入。源库必须经由 `sqlite3` 连接读取：在同一进程里用 `open()` / `shutil.copy` 打开再关闭数据库文件，会释放该进程中 SQLite 连接持有的 POSIX 文件锁，可能损坏正在使用的数据库。

```python
"""ForgeMesh SQLite 备份工具。"""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


async def backup_database(db_path: Path, backup_dir: Path) -> Path:
    """在线备份单个 SQLite 数据库：备份 API 快照 + 完整性校验 + 摘要。

    Args:
        db_path: 源数据库文件路径。
        backup_dir: 备份目录。

    Returns:
        备份文件路径。
    """
    def _backup() -> Path:
        # sqlite3.connect 会为不存在的路径新建空库
        if not db_path.is_file():
            raise FileNotFoundError(f"database not found: {db_path}")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{db_path.stem}-{timestamp}.db"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Step 1: 在线备份
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        # Step 2: 完整性校验
        verify_conn = sqlite3.connect(str(backup_path))
        result = verify_conn.execute("PRAGMA integrity_check").fetchone()
        verify_conn.close()
        if result[0] != "ok":
            raise RuntimeError(f"backup integrity check failed: {backup_path}")

        # Step 3: 摘要文件
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
| 2 | 选择备份版本 | 从 `/var/backups/forgemesh/` 选择最近的有效备份 | < 1min |
| 3 | 校验备份摘要 | `sha256sum -c <backup>.db.sha256` | < 30s |
| 4 | 校验备份完整性 | `sqlite3 <backup>.db "PRAGMA integrity_check;"` 输出 `ok` | < 30s |
| 5 | 保留当前（损坏的）数据库 | `mv data/forgemesh.db data/forgemesh.db.broken`；`-wal` / `-shm` 若存在，一并改名为 `data/forgemesh.db.broken-wal` / `data/forgemesh.db.broken-shm`（`-wal` 里可能有尚未合并回主库的已提交事务） | < 10s |
| 6 | 恢复备份文件 | `cp <backup>.db data/forgemesh.db` | < 1min |
| 7 | 确认没有残留 WAL/SHM | `rm -f data/forgemesh.db-wal data/forgemesh.db-shm`：旧库的 `-wal` 留在原处会被当成恢复后数据库的日志，两者错配会损坏数据库 | < 10s |
| 8 | 启动控制平面进程 | `systemctl start forgemesh-controlplane` | < 2min |
| 9 | 验证服务健康 | `curl http://localhost:8080/healthz` 返回 200 | < 30s |
| 10 | 验证数据完整性 | 执行数据一致性检查脚本 | < 5min |

### 3.2 恢复脚本

```bash
#!/usr/bin/env bash
# forgemesh-restore.sh — SQLite 数据库恢复脚本
# 用法: ./forgemesh-restore.sh <backup_file> <target_db_path>
# 先停止控制平面：进程仍打开着数据库时替换文件，会丢失数据甚至损坏数据库。
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

# Step 3: 保留当前数据库（若存在），-wal / -shm 随之改名：
# -wal 里可能有尚未合并回主库的已提交事务，也不能留给恢复后的数据库
if [[ -f "${TARGET_PATH}" ]]; then
  BROKEN_PATH="${TARGET_PATH}.broken.$(date -u +%s)"
  mv "${TARGET_PATH}" "${BROKEN_PATH}"
  for suffix in -wal -shm; do
    if [[ -f "${TARGET_PATH}${suffix}" ]]; then
      mv "${TARGET_PATH}${suffix}" "${BROKEN_PATH}${suffix}"
    fi
  done
fi

# Step 4: 恢复备份
cp "${BACKUP_FILE}" "${TARGET_PATH}"

# Step 5: 清除残留 WAL/SHM
rm -f "${TARGET_PATH}-wal" "${TARGET_PATH}-shm"

echo "restore completed: ${BACKUP_FILE} -> ${TARGET_PATH}"
```

### 3.3 全量恢复示例

```bash
# 替换数据库文件前先停止控制平面
systemctl stop forgemesh-controlplane

./forgemesh-restore.sh /var/backups/forgemesh/forgemesh-20260101T120000Z.db /var/lib/forgemesh/data/forgemesh.db

systemctl start forgemesh-controlplane

# 验证健康与权威存储可用
curl -sf http://localhost:8080/healthz | jq .
curl -sf http://localhost:8080/readyz | jq .
```

---

## 4. 灾备演练检查清单

> **演练频率**：每月 1 次。演练在隔离环境执行，不影响生产。

### 4.1 演练前准备

- [ ] 确认演练环境与生产环境配置一致（SQLite 版本、schema、WAL 模式）
- [ ] 备份当前演练环境数据（演练可回滚）
- [ ] 通知相关团队演练时间窗口

### 4.2 备份验证

- [ ] 控制平面运行且有写入时，执行 `forgemesh-backup.sh` 生成一份备份
- [ ] 验证备份文件摘要：`sha256sum -c <backup>.db.sha256` 通过
- [ ] 验证备份完整性：`PRAGMA integrity_check` 返回 `ok`
- [ ] 验证备份可打开：`sqlite3 <backup>.db ".tables"` 列出 `allocations`、`processed_dedup_keys`、`resource_changes`、`resources`
- [ ] 验证备份数据行数与源库一致（抽样表）

### 4.3 恢复验证

- [ ] 停止控制平面进程
- [ ] 执行 `forgemesh-restore.sh` 恢复控制平面数据库
- [ ] 启动控制平面进程
- [ ] `/healthz` 探针返回 200
- [ ] `/readyz` 探针返回 200
- [ ] 资源注册表：`list` 返回预期资源数量
- [ ] 审计日志 / 决策记录（仅当部署把它们持久化到文件时）：`query_by_tenant` / `list_by_tenant` 返回预期记录数量

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
| WAL 文件丢失 | 数据缺失 / `PRAGMA integrity_check` 非 `ok` | 从最近有效备份恢复；`-wal` 中尚未合并回主库的事务无法找回 |
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