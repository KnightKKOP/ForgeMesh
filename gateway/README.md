# ForgeMesh Go API 网关

ForgeMesh 的**统一外部入口**与**高性能流式代理**。与 Python 控制平面分离部署，负责
入口关注点（API key 认证、限流边车、SSE / 流式 Token 转发、协议与版本转换）。

> 权威依据：`docs/architecture/development-plan.md` §2.5（WS-4）与 §5.8（Go 网关接口）。
> 硬约束：**仅使用 Go 标准库**，`go.mod` 不引入任何第三方模块，保证 `go build ./...` 一次通过。

---

## 1. 职责边界

网关只做"入口"的事，不做"权威状态"的事。与 Python 控制平面的边界如下（源自 §5.8）：

| 关注点 | Go 网关 | Python 控制平面 |
| --- | --- | --- |
| 外部入口 / 路由 / 版本前缀 | ✅ 负责 | ❌ 不直接暴露 |
| 认证（API key 校验） | ✅ 校验凭证、覆写租户头、推理路由检查 `read` scope | ✅ 用同一份 key 文件独立复核凭证、租户与 scope（**不信任单点**） |
| 限流（租户/路由） | ✅ 按已验证租户的令牌桶 | 业务级配额（Policy / Quota） |
| SSE / 流式 Token 转发 | ✅ 负责 | 提供上游流 |
| 协议 / 版本转换 | ✅ 负责 | 提供 `v1alpha1` 内部契约 |
| 资源权威状态 / 协调 / 工作流 | ❌ 不涉及 | ✅ 负责 |
| 探针 | ✅ `/healthz`、`/readyz` | ✅ `/healthz`、`/readyz` |

> **零信任提示**：网关校验 API key 后，以 key 绑定的租户覆写租户头再转发；控制平面
> 不信任这个头，而是用调用方的 `Authorization` 自行校验（[ADR-014](../docs/design-decisions/adr-014.md)）。
> 详见 `internal/middleware/authz.go` 与 `internal/middleware/apikey.go`。

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
| ANY | `/api/v1/inference/{svc}/chat` | **SSE 流式代理** | 全链 + `read` scope |

`{svc}` 为 Go 1.22 `net/http.ServeMux` 的路径变量语法，表示单个路径段（推理服务标识）。

### 中间件链

执行顺序（外层 → 内层，与 `router.go` 实际组装一致，并由
`internal/middleware/chain_test.go` 验证）：

```
Recovery → Correlation → Authenticate → RateLimit → Route
```

- **Recovery**：兜底 panic → 500，记录堆栈，进程不退出（最外层，覆盖全量请求）。
- **Correlation**：缺失则用 `crypto/rand` 生成 128-bit hex 关联 ID，注入 context 与响应头；已携带则透传。
- **Authenticate**：按 `GATEWAY_AUTH_MODE` 校验凭证——`apikey`（默认）以 `Authorization: Bearer <key>` 的 SHA-256 摘要查 key 文件，`dev` 直接信任租户头。已验证的主体（subject / 租户 / scope）注入 context，并以已验证的租户**覆写**入站租户头。缺少或无效凭证返回 `401`（带 `WWW-Authenticate: Bearer realm="forgemesh"`），租户头与 key 绑定的租户不一致返回 `403`，响应体均为 JSON 错误信封。
- **RateLimit**：按已验证租户的令牌桶限流，超限 `429 + Retry-After`；位于 Authenticate 之后，被拒请求不消耗令牌；不读取租户头或 `X-Forwarded-For`。

> 说明：探针刻意不经过认证/限流，保证负载均衡健康检查始终可用。

### Header 转发与剥离策略

两个转发方向的 header 策略（受信来源与处理动作）：

**方向一：客户端 → 网关 → Python 控制平面**（资源类反代，`upstream/client.go`）

| Header | 策略 | 受信来源与说明 |
| --- | --- | --- |
| `X-Tenant-ID`（可经 `GATEWAY_TENANT_HEADER` 配置） | **覆写后转发** | Authenticate 以已验证的租户**覆写**入站头再放行（`apikey` 模式取自 key；`dev` 模式取 TrimSpace 后首个值），下游读到的与认证上下文严格同源；`apikey` 模式下任一取值与 key 不一致即返回 `403` |
| `Authorization` | 透传 | 控制平面用同一份 key 文件独立复核调用方（ADR-014） |
| `X-Correlation-ID`（可配置） | 生成/透传 | Correlation 中间件受信：缺失则生成 128-bit hex，已携带则原样透传（跨服务链路连续） |
| `X-Forwarded-*`（For/Proto/Host） | 网关注入 | ReverseProxy `SetXForwarded` 标准注入，标记经代理的事实 |
| `Host`、`Content-Length` | 剥离后重写 | 由出站 Transport 按上游语义重写，避免长度/主机不一致 |
| hop-by-hop 头（`Connection`/`Keep-Alive`/`TE`/`Upgrade` 等） | 剥离 | 逐跳语义不跨节点转发 |

**方向二：客户端 → 网关 → 推理实例**（SSE 流式代理，`stream/proxy.go`）

| Header | 策略 | 受信来源与说明 |
| --- | --- | --- |
| `X-Tenant-ID` | 覆写后转发 | 同方向一：Authenticate 写入的已验证租户，同时作为 Resolver 查询与多租户缓存隔离的 key |
| `X-Correlation-ID` | 透传 | 由 Correlation 中间件保证存在（缺失时已生成） |
| `Authorization`、`Cookie` | **剥离** | 调用方的平台凭证只交给网关与控制平面，不交给推理实例；Resolver 查询控制平面时携带调用方的 `Authorization`，控制平面拒绝（`403`）时网关同样返回 `403` |
| `Accept` | **强制覆写** | 网关强制 `text/event-stream`，保证流式语义 |
| `Host`、`Content-Length` | 剥离后重写 | 同方向一 |
| hop-by-hop 头（双向） | 剥离 | 请求与响应两个方向均过滤 |

**响应方向（上游/实例 → 客户端）**：非 hop-by-hop 头透传；流式响应强制
`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。

**安全缺陷记录（WP-D1，已修复）**：修复前 Authz 仅校验租户头存在性、不回写
请求头，下游（反代/流式代理/限流 key）重新读取客户端**原始头**——多值头
（两条 `X-Tenant-ID`）或空白填充值可造成"鉴权身份 ≠ 转发身份"的横向越权
（confused deputy）。最小防御修复：校验通过后以受信解析值覆写入站租户头
（`internal/middleware/authz.go`），回归测试见 `internal/router/tenant_security_test.go`。

**认证缺失（ADR-014，已修复）**：WP-D1 之后租户身份仍是"客户端声明"——网关只检查
租户头是否存在，控制平面直接信任该头且可被直连，限流键同样取自客户端可控的头，流式
代理把调用方的 `Authorization` / `Cookie` 原样交给实例。现网关与控制平面各自校验绑定
租户的 API key，限流按已验证租户计数，实例不再收到调用方凭证；回归测试见
`internal/router/auth_test.go`、`internal/middleware/authz_test.go` 与
`tests/unit/controlplane/test_auth.py`。

---

## 3. 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `GATEWAY_LISTEN_ADDR` | `:8081` | 监听地址 |
| `GATEWAY_UPSTREAM_URL` | `http://127.0.0.1:8080` | Python 控制平面地址 |
| `GATEWAY_AUTH_MODE` | `apikey` | `apikey`：校验 `Authorization: Bearer <key>`；`dev`：直接信任租户请求头，只允许回环监听地址（如 `127.0.0.1:8081`），仅供本地开发；其他取值拒绝启动 |
| `GATEWAY_API_KEYS_FILE` | — | `apikey` 模式必填；与控制平面 `FORGEMESH_API_KEYS_FILE` 格式相同（只含 SHA-256 摘要），缺失或格式非法时拒绝启动 |
| `GATEWAY_RATE_LIMIT_RPS` | `50` | 每租户每秒令牌补充速率 |
| `GATEWAY_RATE_LIMIT_BURST` | `100` | 每租户令牌桶容量（突发上限） |
| `GATEWAY_TENANT_HEADER` | `X-Tenant-ID` | 租户请求头：`apikey` 模式下可省略，携带时必须与 key 一致；`dev` 模式下即租户身份 |
| `GATEWAY_CORRELATION_HEADER` | `X-Correlation-ID` | 链路关联 ID 请求头 |
| `GATEWAY_RESOLVER_TTL_SECONDS` | `5` | 推理服务→实例列表缓存 TTL（秒） |
| `GATEWAY_UNHEALTHY_COOLDOWN_SECONDS` | `10` | 实例连续 3 次失败被摘流后的冷却时长（秒） |

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
    │   ├── ratelimit.go            # 按已验证租户的令牌桶（自实现）
    │   ├── authz.go                # Authenticate / RequireScope、Principal、dev 模式认证器
    │   ├── apikey.go               # key 文件解析与 API key 认证器
    │   └── errors.go               # JSON 错误信封
    ├── upstream/client.go          # httputil.ReverseProxy 反代
    ├── upstream/resolver.go        # 推理服务 → 实例 endpoint 解析（TTL 缓存、健康摘流）
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
GATEWAY_API_KEYS_FILE=/path/to/api-keys.json make run   # apikey 模式，默认 :8081
make run-dev         # dev 模式：信任租户头，只监听 127.0.0.1:8081
```

key 与 key 文件条目由控制平面工具生成（见根 README「运行控制平面」）：

```bash
python -m forgemesh.controlplane.keygen --tenant acme --subject ci-bot
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
    CO --> AZ[Authenticate\n校验 API key 并覆写租户头]
    AZ -->|401 / 403| C
    AZ --> RL[RateLimit\n已验证租户令牌桶]
    RL -->|429| C
    RL --> RT{Route}
    RT -->|/healthz /readyz| H[Probe]
    RT -->|/api/v1/resources 等| UP[ReverseProxy]
    RT -->|/api/v1/inference/{svc}/chat| SP[SSE StreamProxy]
    UP --> CP[(Python 控制平面)]
    SP --> CP
```

---

## 8. 后续演进（首期骨架之外）

- 在 API key 之外接入 OIDC/JWT 与 mTLS（新的 `Authenticator` 实现）；key 文件热加载与过期时间（见 ADR-014 后续事项）。
- RateLimit 改为分布式限流（如 Redis），支持多网关实例共享配额。
- 协议/版本转换（`v1alpha1` ↔ 对外版本）与 gRPC-stream 转发。
- OpenTelemetry 链路追踪导出（复用 Correlation ID 作为 trace 关联）。