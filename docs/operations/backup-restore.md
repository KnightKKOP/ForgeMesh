# 备份与灾备演练

> 适用范围：ForgeMesh 控制平面 SQLite 权威状态存储（`FORGEMESH_DB_PATH`），以及部署推理服务时同目录下的工作流存储（`forgemesh.workflows.db`）。
>
> 对齐 ADR-007（注册表为权威状态）、development-plan WP-4.7 安全测试与灾备。

## 1. 概述

ForgeMesh 控制平面（`forgemesh-control-plane`）把权威状态存放在 SQLite 文件中，路径取自 `FORGEMESH_DB_PATH`（默认为工作目录下的 `forgemesh.db`）。设置了 `FORGEMESH_RUNTIME_LAUNCHER`（部署推理服务）时，部署工作流的运行记录存放在同一目录下的第二个文件中：文件名把数据库文件名的扩展名换成 `.workflows.db`（`forgemesh.db` → `forgemesh.workflows.db`，没有扩展名时追加，见 `forgemesh.controlplane.sqlite.workflow_db_path`），不能单独配置。

| 文件 | 表 | 模块 | 内容 |
|------|----|------|------|
| `forgemesh.db` | `resources` / `allocations` / `allocation_devices` / `resource_changes` | `forgemesh.controlplane.registry.SQLiteRegistry` | 资源对象（spec/status/conditions）、资源分配记录及其占用的设备、控制器读取的变更记录 |
| `forgemesh.db` | `processed_dedup_keys` | `forgemesh.controlplane.reconciler.dedup.ProcessedKeyStore` | 控制器已处理的去重键 |
| `forgemesh.db` | `outbox_events` / `audit_events` | `forgemesh.controlplane.outbox.Outbox` / `forgemesh.controlplane.audit.AuditLog`（与 `SQLiteRegistry` 共用连接） | 资源变更事件（待发布、`dead` 与 7 天内已发布的）与 API 写入的审计记录，与资源在同一个事务中写入 |
| `forgemesh.db` | `registry_meta` | `forgemesh.controlplane.pagetokens`（`SQLiteRegistry` 与 `AuditLog` 打开数据库时创建） | 列表分页游标（资源列表与审计记录列表）的签名密钥 |
| `forgemesh.workflows.db` | `workflow_runs` / `workflow_steps` | `forgemesh.workflowruntime.store.WorkflowStore` | 每个推理服务每一代 `spec` 的部署工作流运行（幂等键 `deployment:{uid}:{generation}`，实例丢失后重新部署的第 n 次追加 `:{n}`）及其各步骤的状态与结果（含启动的实例 id 与地址） |

两个文件之间没有共同的事务，备份与恢复时把它们当作一组，见 2.6 节。推理实例本身不在任何文件中：它们活不过控制平面的重启，恢复之后的情况见 3.5 节。`vllm-process` / `tgi-process` 启动器还在同一目录下的 `forgemesh.instances.json` 中记录正在运行的实例进程，控制平面被杀死之后，下次启动时据此结束留下的实例；这个文件不需要备份，也不需要随恢复处理（另一次开机的记录被忽略，记录的进程已不在时只删去记录）。

决策记录（`forgemesh.decision.records.DecisionRecordStore`）等其他 SQLite 存储默认使用内存库，控制平面进程不会打开它们；嵌入方把它们指向文件时，按本文同样的方法逐个备份。

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
- 部署编排中 control-plane 服务**不得扩展 replicas**（见 `deploy/docker/docker-compose.yaml` 顶部注释）；Kubernetes 清单固定 `replicas: 1`，并以 `strategy: Recreate` 避免滚动更新期间新旧 Pod 同时写库。多实例部署（高可用/水平扩展）须先满足 ADR-013 的 PostgreSQL 迁移判据并完成迁移。
- **升级流程**（与本文备份/恢复流程配套）：**停写**（停止控制平面进程）→ **备份**（见 2.2 节）→ **升级**（替换二进制/镜像并应用变更）→ **恢复**（重启进程并验证 `/healthz` 与数据一致性）。

---

## 2. SQLite 备份策略

### 2.1 WAL 模式

控制平面以 WAL（Write-Ahead Logging）模式打开数据库（`forgemesh.controlplane.sqlite.connect`；工作流存储 `WorkflowStore` 同样如此）：写入先追加到 `-wal` 文件，SQLite 再自动 checkpoint 合并回主库文件（`-wal` 每增长约 1000 页一次，最后一个连接关闭时再合并一次）。因此：

- 运行期间已提交的事务可能只在 `-wal` 里。**不要用 `cp` / `rsync` 复制运行中的数据库文件**：复制出的主库缺少这部分事务，复制途中发生的自动 checkpoint 还会改写主库文件，备份可能前后不一致甚至损坏。先执行 `PRAGMA wal_checkpoint` 再复制同样不安全：checkpoint 与复制之间、复制过程中控制平面仍在提交。
- 主库文件与 `-wal` / `-shm` 是一个整体，移动、删除、恢复时一起处理（见第 3 节）。
- 数据库放在本地磁盘上：WAL 依赖共享内存文件 `-shm`，不支持 NFS / SMB 等网络文件系统。Kubernetes 上 PVC 的 StorageClass 应是块存储（云盘等），不能是 NFS 类的共享文件系统。

确认数据库处于 WAL 模式：

```bash
sqlite3 /var/lib/forgemesh/data/forgemesh.db "PRAGMA journal_mode;"  # 应返回 wal
# 工作流存储同样返回 wal；sqlite3 会为不存在的路径新建空文件，先确认它存在
test -f /var/lib/forgemesh/data/forgemesh.workflows.db \
  && sqlite3 /var/lib/forgemesh/data/forgemesh.workflows.db "PRAGMA journal_mode;"
```

### 2.2 备份方法：在线快照

`VACUUM INTO` 在一个读事务内把数据库写成新文件，得到备份开始时刻的一致快照，包含 `-wal` 中已提交的事务；WAL 模式下读不阻塞写，备份期间控制平面照常提交，无需停服。生成的文件不依赖 `-wal`，可以直接复制、归档。

> 不用 sqlite3 命令行的 `.backup`：它每次只复制 100 页，期间其他进程一提交就从头再来，控制平面持续写入时可能一直完不成。

```bash
#!/usr/bin/env bash
# forgemesh-backup.sh — SQLite 数据库在线备份脚本（控制平面无需停服）
# 用法: ./forgemesh-backup.sh <db_path> <backup_dir>
# 数据库旁有工作流存储（<db_path> 去掉 .db 后加 .workflows.db，与 workflow_db_path 一致；
# 数据库文件名须以 .db 结尾或没有扩展名）时紧接着备份它，两份备份共用一个时间戳，是一组（见 2.6 节）
set -euo pipefail

DB_PATH="${1:?usage: $0 <db_path> <backup_dir>}"
BACKUP_DIR="${2:?usage: $0 <db_path> <backup_dir>}"
WORKFLOW_DB_PATH="${DB_PATH%.db}.workflows.db"
TIMESTAMP=$(date -u +"%Y%m%dT%H%M%SZ")

# 路径写错时 sqlite3 会新建一个空库，并"成功"备份这个空库
if [[ ! -f "${DB_PATH}" ]]; then
  echo "ERROR: database not found: ${DB_PATH}" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"

backup() {
  local source=$1 backup_file integrity
  backup_file="${BACKUP_DIR}/$(basename "${source}" .db)-${TIMESTAMP}.db"

  # Step 1: 在线快照
  sqlite3 "${source}" "VACUUM INTO '${backup_file}';"
  # VACUUM INTO 不对生成的文件执行 fsync
  sync "${backup_file}"

  # Step 2: 校验备份完整性（检查出损坏时 sqlite3 的退出码仍为 0，必须比较输出）
  integrity=$(sqlite3 "${backup_file}" "PRAGMA integrity_check;")
  if [[ "${integrity}" != "ok" ]]; then
    echo "ERROR: backup integrity check failed: ${integrity}" >&2
    exit 1
  fi

  # Step 3: 计算摘要（供恢复时校验）
  sha256sum "${backup_file}" > "${backup_file}.sha256"

  echo "backup completed: ${backup_file}"
}

# 先注册表、后工作流存储（见 2.6 节）
backup "${DB_PATH}"
if [[ -f "${WORKFLOW_DB_PATH}" ]]; then
  backup "${WORKFLOW_DB_PATH}"
fi
```

### 2.3 备份调度（cron）

```cron
# 每 5 分钟备份一次控制平面数据库（路径与 FORGEMESH_DB_PATH 一致；旁边的工作流存储随之备份）
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


async def backup_database(
    db_path: Path, backup_dir: Path, timestamp: str | None = None
) -> Path:
    """在线备份单个 SQLite 数据库：备份 API 快照 + 完整性校验 + 摘要。

    Args:
        db_path: 源数据库文件路径。
        backup_dir: 备份目录。
        timestamp: 备份文件名中的时间戳，默认取当前时间。同一组备份（先注册表、后工作流
            存储，见 2.6 节）传入同一个值，3.2 的恢复脚本按它配对。

    Returns:
        备份文件路径。
    """
    def _backup() -> Path:
        # sqlite3.connect 会为不存在的路径新建空库
        if not db_path.is_file():
            raise FileNotFoundError(f"database not found: {db_path}")
        stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"{db_path.stem}-{stamp}.db"
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

### 2.5 容器部署中的备份

控制平面镜像不含 `sqlite3` 命令行工具，根文件系统只读，2.2 的脚本无法在容器内运行。在容器内用备份 API 把快照写到数据卷上的 `backup/` 目录（数据卷需留出与数据库同样大小的空间），先注册表、后工作流存储（见 2.6 节），取出后在宿主机上校验。`compose` 使用启动时的同一组 `-f` 文件（叠加了 `docker-compose.fake-runtime.yaml` 时也要带上）：

```bash
compose=(docker compose -f deploy/docker/docker-compose.yaml)
"${compose[@]}" exec control-plane python -c "
import pathlib, sqlite3
data = pathlib.Path('/var/lib/forgemesh')
(data / 'backup').mkdir()
for name in ('forgemesh.db', 'forgemesh.workflows.db'):
    if (data / name).is_file():  # 没有部署推理服务时没有工作流存储
        src = sqlite3.connect(data / name)
        dst = sqlite3.connect(data / 'backup' / name)
        src.backup(dst)
        dst.close()
        src.close()
"
"${compose[@]}" cp control-plane:/var/lib/forgemesh/backup/. ./forgemesh-backup
"${compose[@]}" exec control-plane rm -r /var/lib/forgemesh/backup
for db in ./forgemesh-backup/*.db; do
  echo "$db: $(sqlite3 "$db" "PRAGMA integrity_check;")"   # 每个文件都必须为 ok
done
(cd ./forgemesh-backup && sha256sum ./*.db > SHA256SUMS)
```

`./forgemesh-backup` 目录就是一组备份，文件保留原名，恢复见 3.4 节。Kubernetes 上用 `kubectl -n forgemesh exec deploy/forgemesh-control-plane -- python -c "…"` 执行同样的快照命令，再用 `kubectl -n forgemesh cp <pod>:/var/lib/forgemesh/backup ./forgemesh-backup` 取出、`kubectl -n forgemesh exec deploy/forgemesh-control-plane -- rm -r /var/lib/forgemesh/backup` 清理，之后同样在宿主机上逐个校验并生成 `SHA256SUMS`（`kubectl cp` 需要 Pod 名称；它依赖的 `tar` 在镜像中）。

### 2.6 注册表与工作流存储的一致性

部署推理服务时，注册表（`forgemesh.db`）与工作流存储（`forgemesh.workflows.db`）各自的快照都是一致的，但两个文件之间没有共同的事务：只有控制平面停止时备份的两个文件是同一时刻的状态。在线备份时先快照注册表、紧接着快照工作流存储（2.2 的脚本与 2.5 的命令都按这个顺序）。控制平面总是先在工作流存储中记录部署运行的进展，再在注册表中写入依赖它的 phase，因此恢复后的注册表引用的部署运行都在恢复后的工作流存储中，且不会比注册表记录的更旧。

例外是两次快照之间才开始部署的服务：注册表快照中它还没有分配（或者根本还不存在），工作流存储快照中却已有它未结束的部署运行，恢复之后控制平面启动时继续这个运行。快照时实例已经启动的，运行在验证实例时失败，服务（若存在）随之进入 `Failed`；还没有启动实例的，运行在两次快照之间分配到的设备上启动实例，而恢复后的注册表没有这份分配，可能把这些设备再分配给其他服务；服务本身不存在时，这个实例没有任何服务引用，一直运行到控制平面下一次停止（之后运行已结束，不会再继续）。需要排除这些情况时，停止控制平面之后再备份。

恢复时两个文件必须取自同一组备份（3.2 的脚本按备份文件名配对，3.4 按目录）。只恢复注册表、保留更新的工作流存储，相当于把两次快照的间隔拉长到备份之后的全部时间，备份之后开始部署的服务都会出现上面的情况。一组备份中没有工作流存储时（备份时还没有部署推理服务），把现有的工作流存储移走（3.2 的脚本与 3.4 的命令都会这样做），控制平面启动时新建一个空的：注册表中仍在部署的服务找不到部署运行，进入 `Failed`（`WorkflowRunNotFound`），修改 `spec` 或删除后重新创建即可再次部署。

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

部署推理服务时，第 3–7 步对同一组备份中的工作流存储（`forgemesh.workflows-<时间戳>.db` → `data/forgemesh.workflows.db`）再执行一次；这组备份中没有工作流存储时，第 5 步照常移走现有的工作流存储，跳过第 6 步（见 2.6 节）。3.2 的脚本自动完成这些步骤。恢复之后推理服务的状态见 3.5 节。

### 3.2 恢复脚本

```bash
#!/usr/bin/env bash
# forgemesh-restore.sh — SQLite 数据库恢复脚本
# 用法: ./forgemesh-restore.sh <backup_file> <target_db_path>
# 同时恢复同一组备份中的工作流存储（2.2 的脚本写在旁边的 *.workflows-<时间戳>.db），
# 这组备份中没有工作流存储时移走现有的工作流存储（见 2.6 节）。
# 先停止控制平面：进程仍打开着数据库时替换文件，会丢失数据甚至损坏数据库。
set -euo pipefail

BACKUP_FILE="${1:?usage: $0 <backup_file> <target_db_path>}"
TARGET_PATH="${2:?usage: $0 <backup_file> <target_db_path>}"
WORKFLOW_BACKUP_FILE="${BACKUP_FILE%-*}.workflows-${BACKUP_FILE##*-}"
WORKFLOW_TARGET_PATH="${TARGET_PATH%.db}.workflows.db"
BROKEN_SUFFIX=".broken.$(date -u +%s)"

# Step 1: 校验摘要；Step 2: 校验完整性
verify() {
  local integrity
  sha256sum -c "$1.sha256"
  integrity=$(sqlite3 "$1" "PRAGMA integrity_check;")
  if [[ "${integrity}" != "ok" ]]; then
    echo "ERROR: backup integrity check failed: $1: ${integrity}" >&2
    exit 1
  fi
}

# Step 3: 保留当前数据库（若存在），-wal / -shm 随之改名：
# -wal 里可能有尚未合并回主库的已提交事务，也不能留给恢复后的数据库
keep() {
  local suffix
  if [[ -f "$1" ]]; then
    mv "$1" "$1${BROKEN_SUFFIX}"
    for suffix in -wal -shm; do
      if [[ -f "$1${suffix}" ]]; then
        mv "$1${suffix}" "$1${BROKEN_SUFFIX}${suffix}"
      fi
    done
  fi
}

restore() {
  keep "$2"
  # Step 4: 恢复备份
  cp "$1" "$2"
  # Step 5: 清除残留 WAL/SHM
  rm -f "$2-wal" "$2-shm"
  echo "restore completed: $1 -> $2"
}

# 先校验这组备份中的全部文件，任何一个不通过都不改动现有文件
verify "${BACKUP_FILE}"
if [[ -f "${WORKFLOW_BACKUP_FILE}" ]]; then
  verify "${WORKFLOW_BACKUP_FILE}"
fi

restore "${BACKUP_FILE}" "${TARGET_PATH}"
if [[ -f "${WORKFLOW_BACKUP_FILE}" ]]; then
  restore "${WORKFLOW_BACKUP_FILE}" "${WORKFLOW_TARGET_PATH}"
else
  keep "${WORKFLOW_TARGET_PATH}"
  rm -f "${WORKFLOW_TARGET_PATH}-wal" "${WORKFLOW_TARGET_PATH}-shm"
  echo "no workflow store in this backup set; the control plane starts with an empty one"
fi
```

### 3.3 全量恢复示例

```bash
# 替换数据库文件前先停止控制平面
systemctl stop forgemesh-controlplane

# 同一组备份中有 forgemesh.workflows-20260101T120000Z.db 时一并恢复到 data/forgemesh.workflows.db
./forgemesh-restore.sh /var/backups/forgemesh/forgemesh-20260101T120000Z.db /var/lib/forgemesh/data/forgemesh.db

systemctl start forgemesh-controlplane

# 验证健康与权威存储可用
curl -sf http://localhost:8080/healthz | jq .
curl -sf http://localhost:8080/readyz | jq .
```

### 3.4 容器部署中的恢复

容器内同样用备份 API 写回：SQLite 打开目标库时会先处理遗留的 `-wal`，写回在一个事务内完成，不会出现 3.1 第 7 步所说的 WAL 错配。先在宿主机上校验 2.5 取出的一组备份（`(cd ./forgemesh-backup && sha256sum -c SHA256SUMS)`，再按 3.1 第 4 步逐个检查完整性），把这组文件以原名放在 `./restore/` 下（对 uid 10001 可读，例如权限 0644）。`compose` 使用启动时的同一组 `-f` 文件：

```bash
compose=(docker compose -f deploy/docker/docker-compose.yaml)
"${compose[@]}" stop control-plane
# 保留当前数据目录（含可能存在的 -wal / -shm）
"${compose[@]}" cp control-plane:/var/lib/forgemesh ./forgemesh-data.broken
# 一次性容器挂载同一个数据卷。备份 API 生成的文件仍标记为 WAL 模式，
# 不加 immutable=1 时 SQLite 要在只读的 /restore 中创建 -wal / -shm，打开即失败
"${compose[@]}" run --rm --no-deps -v "$PWD/restore:/restore:ro" control-plane python -c "
import pathlib, sqlite3
data = pathlib.Path('/var/lib/forgemesh')

def restore(name):
    src = sqlite3.connect(f'file:/restore/{name}?immutable=1', uri=True)
    dst = sqlite3.connect(data / name)
    src.backup(dst)
    dst.close()
    src.close()

restore('forgemesh.db')
if pathlib.Path('/restore/forgemesh.workflows.db').is_file():
    restore('forgemesh.workflows.db')
else:  # 这组备份中没有工作流存储：删除现有的（上面已保留副本），见 2.6 节
    for suffix in ('', '-wal', '-shm'):
        (data / f'forgemesh.workflows.db{suffix}').unlink(missing_ok=True)
"
"${compose[@]}" up --detach --wait control-plane   # 等 /readyz 健康检查通过
curl -sf http://127.0.0.1:8080/readyz
```

Kubernetes 上先 `kubectl -n forgemesh scale deploy/forgemesh-control-plane --replicas=0` 并等 Pod 退出，再启动一个挂载 PVC `forgemesh-control-plane-data`、以 uid 10001 运行的临时 Pod（同一镜像，命令如 `sleep infinity`）：用 `kubectl cp` 取出 `/var/lib/forgemesh` 保留当前数据、把这组备份文件复制到它的 `/tmp/restore/`，`kubectl exec` 执行上面的恢复命令（源路径中的 `/restore` 改为 `/tmp/restore`），删除临时 Pod 后把副本数改回 1。

### 3.5 恢复之后的推理服务

恢复总要重启控制平面，而推理实例活不过控制平面的重启（`fake` 启动器的实例在控制平面进程内；`vllm-process` / `tgi-process` 的实例是它的子进程，控制平面停止时停止它们，控制平面被杀死时留下的在下次启动时结束），恢复之后没有任何实例在运行：

- 恢复前（备份时）`Ready` 的服务在控制平面启动时转为 `Degraded`：`Ready=False`（`InstanceLost`），`status.endpoints` 中的实例标记为 `ready: false`，网关不再转发。随后控制器像处理实例丢失一样，以同一个 `spec` 与服务持有的分配重新部署它（备份时就是 `Degraded` 的服务同样如此），新实例通过验证后回到 `Ready`。
- 仍在部署（含 `Updating`）的服务从中断处继续：还没有启动实例的照常完成；实例已经启动、还在验证的，运行在验证实例时失败，服务进入 `Failed`（`WorkflowFailed`），修改 `spec` 或删除后重新创建即可再次部署；部署运行已经成功、只是还没写入 `Ready` 的重新部署。
- 注册表与工作流存储不是同一组备份时的情况见 2.6 节。

### 3.6 恢复之后的事件与审计记录

资源变更事件（`outbox_events`）与审计记录（`audit_events`）和资源在同一个文件中、由同一个事务写入，恢复之后三者是同一时刻的状态：

- 备份之后的资源写入连同它们的事件与审计记录一起丢失。
- 备份时仍待发布的事件在控制平面启动后发布，其中包括备份之后已经送达过的：接收方会再次收到它们（事件本就至少投递一次），按 `eventId` 去重即可。
- 备份时已是 `dead` 的事件仍为 `dead`，需要时按第 5 节重新投递。
- 控制平面启动后服务转为 `Degraded` 并重新部署（3.5 节）：这些 status 写入照常写入新的事件（`inference.service.status_updated`），与其他控制器的写入一样不写审计记录。

---

## 4. 灾备演练检查清单

> **演练频率**：每月 1 次。演练在隔离环境执行，不影响生产。

### 4.1 演练前准备

- [ ] 确认演练环境与生产环境配置一致（SQLite 版本、schema、WAL 模式）
- [ ] 备份当前演练环境数据（演练可回滚）
- [ ] 通知相关团队演练时间窗口

### 4.2 备份验证

- [ ] 控制平面运行且有写入时，执行 `forgemesh-backup.sh` 生成一份备份（容器部署按 2.5 节）
- [ ] 验证备份文件摘要：`sha256sum -c <backup>.db.sha256` 通过
- [ ] 验证备份完整性：`PRAGMA integrity_check` 返回 `ok`
- [ ] 验证备份可打开：`sqlite3 <backup>.db ".tables"` 列出 `allocation_devices`、`allocations`、`audit_events`、`outbox_events`、`processed_dedup_keys`、`registry_meta`、`resource_changes`、`resources`
- [ ] 部署推理服务时：同一组备份中有工作流存储（时间戳相同的 `forgemesh.workflows-<时间戳>.db`，容器部署为同一目录中的 `forgemesh.workflows.db`），`.tables` 列出 `workflow_runs`、`workflow_steps`
- [ ] 验证备份数据行数与源库一致（抽样表）

### 4.3 恢复验证

- [ ] 停止控制平面进程
- [ ] 执行 `forgemesh-restore.sh` 恢复控制平面数据库（容器部署按 3.4 节），部署推理服务时工作流存储取自同一组备份
- [ ] 启动控制平面进程
- [ ] `/healthz` 探针返回 200
- [ ] `/readyz` 探针返回 200
- [ ] 资源注册表：`list` 返回预期资源数量
- [ ] 推理服务（3.5 节）：备份时 `Ready` 的服务经 `Degraded`（`Ready=False`，`InstanceLost`）重新部署，回到 `Ready`，`status.endpoints` 中的 `instanceId` 与备份时不同
- [ ] 审计记录：`GET /api/v1/audit-events` 返回备份时已有的记录
- [ ] 决策记录（仅当部署把它持久化到文件时）：`list_by_tenant` 返回预期记录数量

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
| 注册表与工作流存储来自不同的备份 | 恢复后仍在部署的服务进入 `Failed`（`WorkflowRunNotFound`），或设备被注册表之外的实例占用（2.6 节） | 停止控制平面，从同一组备份重新恢复两个文件 |
| 租户数据串扰 | 安全测试失败 | 立即从备份恢复 + 排查隔离边界缺陷 |
| 事件接收方（webhook）长时间不可用 | ERROR 日志 `outbox event … is dead after 10 failed attempts` | 修复接收方后重新投递 `dead` 事件（见下） |

`dead` 事件不再自动发送。下面的命令只改动 `outbox_events` 中的 `dead` 行（一次短事务），可以在控制平面运行时执行，Dispatcher 在下一轮发布放回队列的事件。路径须与 `FORGEMESH_DB_PATH` 一致（路径写错时会新建一个空库）：

```bash
# 列出 dead 事件（最多 100 条）：id、类型、失败次数与最近一次错误
.venv/bin/python -c "
from forgemesh.controlplane.outbox import Outbox
for e in Outbox('./forgemesh.db').list_dead(): print(e.event_id, e.event_type, e.attempts, e.last_error)"
# 全部放回待发布队列（失败次数清零），输出放回的事件数
.venv/bin/python -c "from forgemesh.controlplane.outbox import Outbox; print(Outbox('./forgemesh.db').redrive())"
```

容器部署中以 `docker compose -f deploy/docker/docker-compose.yaml exec control-plane python -c "…"` 执行同样的命令，数据库路径为 `/var/lib/forgemesh/forgemesh.db`。

---

## 6. 相关文档

- [架构设计](../architecture/architecture.md) — 权威状态存储与多租户隔离
- [开发计划](../architecture/development-plan.md) — WP-4.7 安全测试与灾备
- [ADR-007](../design-decisions/adr-007.md) — 注册表为权威状态
- [安全测试套件](../../tests/security/) — 租户隔离 / 插件权限 / 密钥脱敏