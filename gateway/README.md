# ForgeMesh Go API 网关

ForgeMesh 的**统一外部入口**与**高性能流式代理**。与 Python 控制平面分离部署，负责
入口关注点（鉴权卸载、限流边车、SSE / 流式 Token 转发、协议与版本转换）。

> 权威依据：`docs/architecture/development-plan.md` §2.5（WS-4）与 §5.8（Go 网关接口）。
> 硬约束：**仅使用 Go 标准库**，`go.mod` 不引入任何第三方模块，保证 `go build ./...` 一次通过。

---

## 1. 职责边界

网关只做"入口"的事，不做"权威状态"的事。与 Python 控制平面的边界如下（源自 §5.8）：

| 关注点 | Go 网关 | Python 控制平面 |
| --- | --- | --- |
| 外部入口 / 路由 / 版本前缀 | ✅ 负责 | ❌ 不直接暴露 |
| 鉴权（Token 校验）卸载 | ✅ 负责（认证结果下发） | 复核租户/权限（**不信任单点**） |
| 限流（租户/路由） | ✅ 令牌桶 | 业务级配额（Policy / Quota） |
| SSE / 流式 Token 转发 | ✅ 负责 | 提供上游流 |
| 协议 / 版本转换 | ✅ 负责 | 提供 `v1alpha1` 内部契约 |
| 资源权威状态 / 协调 / 工作流 | ❌ 不涉及 | ✅ 负责 |
| 探针 | ✅ `/healthz`、`/readyz` | ✅ `/healthz`、`/readyz` |

> **零信任提示**：网关仅完成入口鉴权卸载，把租户身份以受信头下发给下游；
> 下游控制平面**必须**对租户进行二次校验，禁止仅凭网关转发的头就信任租户。
> 详见 `internal/middleware/authz.go`。

---

## 2. 路由表

| 方法 | 路径 | 处理器 | 中间件 |
| --- | --- | --- | --- |
| GET | `/healthz` | 存活探针（始终 200） | 仅 Recovery |
| GET | `/readyz` | 就绪探针（TCP 探测上游，不可达 503） | 仅 Recovery |
| ANY | `/api/v1/resources[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/inference-services[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/workflows[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/decisions[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/inference/{svc}/chat` | **SSE 流式代理** | 全链 |

`{svc}` 为 Go 1.22 `net/http.ServeMux` 的路径变量语法，表示单个路径段（推理服务标识）。

### 中间件链

执行顺序（外层 → 内层）：

```
Recovery → Correlation → RateLimit → Authz → Route
```

- **Recovery**：兜底 panic → 500，记录堆栈，进程不退出（最外层，覆盖全量请求）。
- **Correlation**：缺失则用 `crypto/rand` 生成 128-bit hex 关联 ID，注入 context 与响应头；已携带则透传。
- **RateLimit**：按租户令牌桶限流，超限 `429 + Retry-After`（探针路径豁免）。
- **Authz**：解析租户身份注入 context；缺失返回 `401`（探针路径豁免）。

> 说明：探针刻意不经过鉴权/限流，保证负载均衡健康检查始终可用。

---

## 3. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GATEWAY_LISTEN_ADDR` | `:8081` | 监听地址 |
| `GATEWAY_UPSTREAM_URL` | `http://127.0.0.1:8080` | Python 控制平面地址 |
| `GATEWAY_RATE_LIMIT_RPS` | `50` | 每租户每秒令牌补充速率 |
| `GATEWAY_RATE_LIMIT_BURST` | `100` | 每租户令牌桶容量（突发上限） |
| `GATEWAY_TENANT_HEADER` | `X-Tenant-ID` | 租户身份请求头 |
| `GATEWAY_CORRELATION_HEADER` | `X-Correlation-ID` | 链路关联 ID 请求头 |

---

## 4. 目录结构

```
gateway/
├── go.mod                          # module + go 1.22（无外部依赖）
├── Makefile                        # build / test / run / fmt / vet
├── cmd/gateway/main.go             # 入口：配置 → 路由 → http.Server → 优雅停机
└── internal/
    ├── config/config.go            # 环境变量加载与校验
    ├── middleware/
    │   ├── middleware.go           # Middleware 类型 + Chain 组合子
    │   ├── recovery.go             # panic 兜底 → 500
    │   ├── correlation.go          # 关联 ID 生成/透传 + context 注入
    │   ├── ratelimit.go            # 按租户令牌桶（自实现）
    │   └── authz.go                # 入口鉴权卸载（含零信任注释）
    ├── upstream/client.go          # httputil.ReverseProxy 反代
    ├── stream/proxy.go             # SSE 流式代理（逐块 Flush，断连取消上游）
    ├── health/health.go            # /healthz、/readyz
    └── router/router.go            # 路由表 + 中间件链
```

---

## 5. 本地开发

> 本仓库开发环境可能未安装 Go 工具链；构建与测试由 CI 完成（`.github/workflows/ci.yml` 的 `go` job）。

```bash
cd gateway
make fmt vet build   # 格式化 + 静态检查 + 编译
make test            # go test -race ./...
make run             # 本地启动（默认 :8081）
```

CI 门禁：

```bash
cd gateway
go build ./...
go test ./...
```

---

## 6. 优雅停机

`cmd/gateway/main.go` 使用 `signal.NotifyContext` 监听 `SIGINT`/`SIGTERM`：

1. 信号到达 → 停止接收新连接（`http.Server.Shutdown` 内部停止监听）。
2. 在 30s 排空窗口内等待在途请求（含 SSE 流）自然结束。
3. 超时则强制 `Close`。

流式请求在客户端断开时，通过请求 `context` 自动取消上游请求，避免连接泄漏。

---

## 7. 请求生命周期

```mermaid
flowchart LR
    C[Client] --> R[Recovery]
    R --> CO[Correlation]
    CO --> RL[RateLimit\n租户令牌桶]
    RL -->|429| C
    RL --> AZ[Authz\n解析租户]
    AZ -->|401| C
    AZ --> RT{Route}
    RT -->|/healthz /readyz| H[Probe]
    RT -->|/api/v1/resources 等| UP[ReverseProxy]
    RT -->|/api/v1/inference/{svc}/chat| SP[SSE StreamProxy]
    UP --> CP[(Python 控制平面)]
    SP --> CP
```

---

## 8. 后续演进（首期骨架之外）

- Authz 接入真实 JWT/OIDC 校验（签名、有效期、scope）与 mTLS 信任边界。
- RateLimit 改为分布式限流（如 Redis），支持多网关实例共享配额。
- 协议/版本转换（`v1alpha1` ↔ 对外版本）与 gRPC-stream 转发。
- OpenTelemetry 链路追踪导出（复用 Correlation ID 作为 trace 关联）。