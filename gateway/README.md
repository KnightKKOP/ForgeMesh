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
| 探针与指标 | ✅ `/healthz`、`/readyz`、`/metrics`（网关自己的指标） | ✅ `/healthz`、`/readyz`、`/metrics` |

> **零信任提示**：网关校验 API key 后，以 key 绑定的租户覆写租户头再转发；控制平面
> 不信任这个头，而是用调用方的 `Authorization` 自行校验（[ADR-014](../docs/design-decisions/adr-014.md)）。
> 详见 `internal/middleware/authz.go` 与 `internal/middleware/apikey.go`。

---

## 2. 路由表

| 方法 | 路径 | 处理器 | 中间件 |
| --- | --- | --- | --- |
| GET | `/healthz` | 存活探针（始终 200） | 仅 Observe、Recovery |
| GET | `/readyz` | 就绪探针（请求控制平面 `/readyz`，非 200 或不可达时 503） | 仅 Observe、Recovery |
| GET | `/metrics` | Prometheus 指标（见「可观测性」；`GET` / `HEAD` 之外返回 `405 METHOD_NOT_ALLOWED`） | 仅 Observe、Recovery |
| ANY | `/api/v1/resources[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/inference-services[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/workflows[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/decisions[/...]` | 反代 → Python 控制平面 | 全链 |
| ANY | `/api/v1/audit-events[/...]` | 反代 → Python 控制平面（审计查询） | 全链 |
| ANY | `/api/v1/namespaces/{ns}/inference/{svc}/chat` | **SSE 流式代理**（命名空间 `{ns}` 中的服务） | 全链 + `read` scope |
| ANY | `/api/v1/inference/{svc}/chat` | **SSE 流式代理**（不带命名空间的服务） | 全链 + `read` scope |
| ANY | `/api/v1/` 下的其他路径 | `404 ROUTE_NOT_FOUND` | 全链 |
| ANY | 其他路径 | `404 ROUTE_NOT_FOUND` | Observe、Recovery、Correlation、TraceContext、AccessLog |

`{ns}`、`{svc}` 为 Go 1.22 `net/http.ServeMux` 的路径变量语法，各表示单个路径段（命名空间与推理服务名）。

推理请求不按入站路径转发。Resolver 从控制平面读取服务的 `status.endpoints`，只保留
`ready` 且协议已知的实例；网关按实例的 `protocol` 把请求转发到对应路径，查询串原样保留，
实例地址带路径前缀时（如 `http://10.0.0.1:8000/llama`）协议路径接在前缀之后：

| `protocol` | 实例上的路径 |
| --- | --- |
| `openai-chat` | `/v1/chat/completions` |
| `tgi-generate` | `/generate_stream` |

网关不转换请求体，客户端按服务实例的协议格式发送请求。

同一服务的并发未命中只查询一次控制平面；"服务不存在 / 未就绪 / 无可用实例"缓存 1 秒
（不超过 `GATEWAY_RESOLVER_TTL_SECONDS`），`403`、其他控制平面错误与网络错误不缓存。

### 实例地址限制

`status.endpoints` 中的地址由控制平面写入，网关在拨号时逐个检查解析出的 IP，主机名之后
解析到别处同样生效：

- 设置了 `GATEWAY_INSTANCE_ALLOWED_CIDRS` 时，只连接其中列出的网段；
- 未设置时拒绝回环、链路本地（含 `169.254.169.254`）、未指定地址、组播，以及已知的云
  元数据服务地址（`100.100.100.200`、`fd00:ec2::254`）。

被拒绝的连接返回 `502 INSTANCE_ADDRESS_NOT_ALLOWED`，并计一次实例失败。实例运行在本机时
需要显式允许回环网段，`make run-dev` 已设置 `GATEWAY_INSTANCE_ALLOWED_CIDRS=127.0.0.0/8,::1/128`。

### 中间件链

执行顺序（外层 → 内层，与 `router.go` 实际组装一致；Recovery 之内的顺序由
`internal/middleware/chain_test.go` 验证）：

```
Observe → Recovery → Correlation → TraceContext → AccessLog → Authenticate → RateLimit → LimitRequestBody → Route
```

- **Observe**：按路由类别记录请求数与耗时（见「可观测性」），位于最外层：Recovery 兜底的 500 与被认证、限流拒绝的请求都计入。
- **Recovery**：兜底全量请求的 panic，日志带关联 ID、trace-id 与堆栈；响应头尚未写出时返回 `500 INTERNAL_ERROR`，已写出时中止响应（见「错误响应」）。
- **Correlation**：缺失则用 `crypto/rand` 生成 128-bit hex 关联 ID，注入 context 与响应头；已携带则透传。
- **TraceContext**：沿用或生成 W3C `traceparent`，trace-id 注入 context（见「可观测性」）。
- **AccessLog**：每个请求一行访问日志，带关联 ID 与 trace-id；位于 Authenticate 之前，被认证或限流拒绝的请求同样记录。
- **Authenticate**：按 `GATEWAY_AUTH_MODE` 校验凭证——`apikey`（默认）以 `Authorization: Bearer <key>` 的 SHA-256 摘要查 key 文件，`dev` 直接信任租户头。已验证的主体（subject / 租户 / scope）注入 context，并以已验证的租户**覆写**入站租户头。缺少或无效凭证返回 `401`（带 `WWW-Authenticate: Bearer realm="forgemesh"`），租户头与 key 绑定的租户不一致返回 `403`，响应体均为 JSON 错误信封。
- **RateLimit**：按已验证租户的令牌桶限流，超限返回 `429 RATE_LIMITED` 与 `Retry-After`；位于 Authenticate 之后，被拒请求不消耗令牌；不读取租户头或 `X-Forwarded-For`。
- **LimitRequestBody**：请求体上限为 `GATEWAY_MAX_REQUEST_BODY_BYTES`（默认 10 MiB）。声明的 `Content-Length` 超限时直接返回 `413 REQUEST_TOO_LARGE`，不访问上游；分块传输的请求体读到上限时，反代与流式代理同样返回 `413`，不计实例失败。

> 说明：探针与 `/metrics` 刻意不经过认证/限流（也不写访问日志），保证负载均衡健康检查与指标抓取始终可用。

### 错误响应

网关自己返回的错误响应都是 JSON 错误信封（`Content-Type: application/json`，结构即 OpenAPI 中的 `Error`）：

```json
{"error": {"code": "RATE_LIMITED", "message": "rate limit exceeded", "correlationId": "ad1cd525116940f682ea882c0c7a6b67", "traceId": "4bf92f3577b34da6a3ce929d0e0e4736"}}
```

`correlationId` 与响应头中的关联 ID 相同，`traceId` 是这个请求的 trace-id（见「可观测性」）。
探针与 `/metrics` 不经过 Correlation 与 TraceContext，它们的错误信封（`/metrics` 的 `405`、
panic 兜底的 `500`）没有这两个字段。客户端按状态码与 `error.code` 区分错误，不要解析 `message`。

| 状态码 | `code` | 场景 |
| --- | --- | --- |
| `401` | `UNAUTHENTICATED` | 缺少或无效凭证（`dev` 模式下为缺少租户头），带 `WWW-Authenticate` |
| `403` | `FORBIDDEN` | 租户头与 key 绑定的租户不一致；推理路由缺少 `read` scope；控制平面拒绝 Resolver 的查询 |
| `404` | `ROUTE_NOT_FOUND` | 没有匹配的路由（有别于控制平面表示资源不存在的 `NOT_FOUND`：客户端据此区分地址错误与资源不存在） |
| `405` | `METHOD_NOT_ALLOWED` | `/metrics` 收到 `GET` / `HEAD` 之外的方法（带 `Allow: GET, HEAD`） |
| `413` | `REQUEST_TOO_LARGE` | 请求体超过 `GATEWAY_MAX_REQUEST_BODY_BYTES` |
| `429` | `RATE_LIMITED` | 超过租户的限流（带 `Retry-After`：攒够一个令牌所需的秒数，向上取整，至少为 `1`） |
| `500` | `INTERNAL_ERROR` | 处理器在写出响应头之前 panic（信封不含 panic 的内容，细节只写入日志） |
| `502` | `UPSTREAM_UNAVAILABLE` | 没有收到控制平面（资源接口反代）或推理实例的响应，如连接失败，或控制平面 30 秒内没有返回响应头 |
| `502` | `UPSTREAM_RESOLVE_FAILED` | Resolver 没能从控制平面取得服务的状态：网络错误、`2xx` / `403` / `404` 之外的状态码，或响应无法读取、不是合法 JSON |
| `502` | `INVALID_UPSTREAM` | 实例地址或协议非法 |
| `502` | `INSTANCE_ADDRESS_NOT_ALLOWED` | 实例地址不在允许连接的范围内（见「实例地址限制」） |
| `503` | `NO_HEALTHY_UPSTREAM` | 推理服务不存在、未就绪或没有可用实例 |

控制平面与推理实例的响应（含错误响应）原样透传，网关不改写。以下响应不是错误信封：探针的
状态文档（如 `/readyz` 的 `503 {"status":"unavailable","reason":"upstream not ready"}`），以及
`net/http` 在路由之前拒绝的请求（如请求行或请求头格式错误），后者是纯文本。处理器在写出响应头
之后 panic 时（如控制平面的响应体中途中断），状态码与已写出的响应体都无法更改，网关中止响应，
客户端读到的是不完整的响应。

### Header 转发与剥离策略

两个转发方向的 header 策略（受信来源与处理动作）：

**方向一：客户端 → 网关 → Python 控制平面**（资源类反代，`upstream/client.go`）

| Header | 策略 | 受信来源与说明 |
| --- | --- | --- |
| `X-Tenant-ID`（可经 `GATEWAY_TENANT_HEADER` 配置） | **覆写后转发** | Authenticate 以已验证的租户**覆写**入站头再放行（`apikey` 模式取自 key；`dev` 模式取 TrimSpace 后首个值），下游读到的与认证上下文严格同源；`apikey` 模式下任一取值与 key 不一致即返回 `403` |
| `Authorization` | 透传 | 控制平面用同一份 key 文件独立复核调用方（ADR-014） |
| `X-Correlation-ID`（可配置） | 生成/透传 | Correlation 中间件受信：缺失则生成 128-bit hex，已携带则原样透传（跨服务链路连续） |
| `traceparent` | 透传/生成 | TraceContext 中间件：恰好一个合法的值原样转发，否则生成新的（见「可观测性」）；Resolver 查询控制平面时同样携带 |
| `tracestate` | 透传/剥离 | 随合法的 `traceparent` 原样转发；网关开始新的追踪时剥离 |
| `X-Forwarded-*`（For/Proto/Host） | 网关注入 | ReverseProxy `SetXForwarded` 标准注入，标记经代理的事实 |
| `Host`、`Content-Length` | 剥离后重写 | 由出站 Transport 按上游语义重写，避免长度/主机不一致 |
| hop-by-hop 头（`Connection`/`Keep-Alive`/`TE`/`Upgrade` 等） | 剥离 | 逐跳语义不跨节点转发 |

**方向二：客户端 → 网关 → 推理实例**（SSE 流式代理，`stream/proxy.go`）

| Header | 策略 | 受信来源与说明 |
| --- | --- | --- |
| `X-Tenant-ID` | 覆写后转发 | 同方向一：Authenticate 写入的已验证租户，同时作为 Resolver 查询与多租户缓存隔离的 key |
| `X-Correlation-ID` | 透传 | 由 Correlation 中间件保证存在（缺失时已生成） |
| `traceparent`、`tracestate` | 透传/生成 | 同方向一：推理实例收到的与控制平面收到的相同 |
| `Authorization`、`Cookie` | **剥离** | 调用方的平台凭证只交给网关与控制平面，不交给推理实例；Resolver 查询控制平面时携带调用方的 `Authorization`，控制平面拒绝（`403`）时网关同样返回 `403` |
| `Accept` | **强制覆写** | 网关强制 `text/event-stream`，保证流式语义 |
| `Host`、`Content-Length` | 剥离后重写 | 同方向一 |
| hop-by-hop 头（双向） | 剥离 | 请求与响应两个方向均过滤 |

**响应方向（上游/实例 → 客户端）**：非 hop-by-hop 头透传；流式代理的 2xx 响应强制
`Content-Type: text/event-stream`，错误响应保留实例的 `Content-Type`（如 JSON 错误体）；
流式代理的响应一律设置 `Cache-Control: no-cache`、`X-Accel-Buffering: no`。网关不在响应中回写
`traceparent`：客户端凭响应头中的关联 ID 或错误信封中的 `traceId` 报障。

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

### 可观测性

**指标**：`/metrics` 以 Prometheus 文本格式（0.0.4）输出网关自己的指标，由 `internal/metrics`
用标准库实现。它与探针在同一个监听地址上，不经过认证、限流与访问日志，输出只有聚合数据：标签
只取路由类别、状态码与固定的枚举值，不含租户、服务名、路径或实例地址；每个指标最多 200 个标签
组合，超出的计入标签全为 `overflow` 的一个序列。指标仍能反映整体负载，只让 Prometheus 所在的
网络访问这个端点，不要经公网入口暴露。

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `forgemesh_gateway_http_requests_total{route,status}` | counter | 处理的请求数，含 Recovery 兜底的 `500` 与被认证、限流拒绝的请求 |
| `forgemesh_gateway_http_request_duration_seconds{route}` | histogram | 请求耗时；流式请求为整个流的时长 |
| `forgemesh_gateway_stream_first_byte_seconds` | histogram | 流式代理从开始处理请求（含解析实例）到把实例的第一个响应体字节写给客户端的时间，只统计实例返回 2xx 的流 |
| `forgemesh_gateway_upstream_errors_total{upstream,kind}` | counter | 上游错误数。`upstream`：`control_plane`（反代与 Resolver 的查询）/ `instance`；`kind`：`connect`（没有收到响应，客户端已断开的请求不计）/ `status`（上游返回 5xx）/ `body`（流在中途中断，或控制平面的响应不是合法 JSON）/ `address_not_allowed` / `invalid_endpoint` |
| `forgemesh_gateway_rate_limited_total` | counter | 被限流拒绝（`429`）的请求数 |
| `forgemesh_gateway_resolver_lookups_total{result}` | counter | Resolver 的查询数。`result`：`hit`（缓存命中，含缓存的"服务不存在 / 未就绪 / 无可用实例"）/ `miss`（向控制平面查询）/ `coalesced`（并入同一服务进行中的查询） |
| `forgemesh_gateway_instances{state}` | gauge | Resolver 缓存中（TTL 内解析过）的实例数：`healthy` 参与轮询，`unhealthy` 因连续失败处于冷却期 |

`route` 按路由表确定，不取请求路径：`probe`（`/healthz`、`/readyz`）、`metrics`、`control_plane`
（上表中反代到控制平面的路由，含审计查询）、`inference`（流式推理代理）、`unmatched`（没有匹配
的路由，含 `/api/v1/` 下的未知路径）。

**追踪上下文**：TraceContext 按 W3C Trace Context 校验 `traceparent`：只接受小写十六进制，
trace-id 或 parent-id 全零、版本 `ff` 都不合法；版本 `00` 必须恰好 55 个字符，更高的版本按前
55 个字符解析，之后若还有内容须以 `-` 分隔。请求恰好带一个合法的值时原样转发，`tracestate`
一并保留；否则（缺失、不合法或有多个）网关开始新的追踪：生成版本 `00`、flags `00`（未采样）的
`traceparent`，剥离 `tracestate`。控制平面、推理实例与 Resolver 对控制平面的查询收到同一个
`traceparent`，控制平面按同样的规则读取它（见根 README「可观测性」）。网关不记录 span，也不
导出追踪（OTLP 导出尚未实现）。

**访问日志**：探针与 `/metrics` 之外的每个请求结束时，网关向标准输出写一行（与网关的其他日志相同）：

```
gateway 2026/10/13 03:53:36 access method=POST path="/api/v1/namespaces/default/inference/llama/chat" status=200 duration=1.234s corr=ad1cd525116940f682ea882c0c7a6b67 trace=4bf92f3577b34da6a3ce929d0e0e4736
```

`path` 不含查询串，流式请求的 `duration` 是整个流的时长；被认证、限流拒绝的请求同样记录；
处理器在写出响应头之前 panic 时记 `status=500`。网关的错误信封（见「错误响应」）与反代、
流式代理的错误日志、Recovery 的 panic 日志（`corr=… trace=…`）带同一组 ID。

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
| `GATEWAY_INSTANCE_ALLOWED_CIDRS` | — | 推理实例允许所在的网段，逗号分隔（如 `10.0.0.0/8,fd00::/8`）；未设置时拒绝回环、链路本地与云元数据地址（见「实例地址限制」）；取值非法时拒绝启动 |
| `GATEWAY_MAX_REQUEST_BODY_BYTES` | `10485760`（10 MiB） | 请求体上限（字节），超出返回 `413 REQUEST_TOO_LARGE`；必须为正整数 |

---

## 4. 目录结构

```
gateway/
├── go.mod                          # module + go 1.22（无外部依赖）
├── Makefile                        # build / test / run / fmt / vet
├── cmd/gateway/main.go             # 入口：配置 → 路由 → http.Server → 优雅停机
└── internal/
    ├── config/config.go            # 环境变量加载与校验
    ├── metrics/
    │   ├── metrics.go              # Prometheus 文本格式：Counter / Histogram / 回调指标，序列上限
    │   ├── gateway.go              # 网关的指标与 /metrics 处理器
    │   └── metricstest/            # 测试用：严格解析文本格式、读取样本取值
    ├── middleware/
    │   ├── middleware.go           # Middleware 类型 + Chain 组合子
    │   ├── observe.go              # 按路由类别记录请求数与耗时
    │   ├── recovery.go             # panic 兜底 → 500 INTERNAL_ERROR
    │   ├── correlation.go          # 关联 ID 生成/透传 + context 注入
    │   ├── tracecontext.go         # W3C traceparent 校验/生成 + context 注入
    │   ├── accesslog.go            # 访问日志（带关联 ID 与 trace-id）
    │   ├── ratelimit.go            # 按已验证租户的令牌桶（自实现）
    │   ├── authz.go                # Authenticate / RequireScope、Principal、dev 模式认证器
    │   ├── apikey.go               # key 文件解析与 API key 认证器
    │   ├── bodylimit.go            # 请求体上限
    │   └── errors.go               # JSON 错误信封（带关联 ID 与 trace-id）
    ├── upstream/client.go          # httputil.ReverseProxy 反代
    ├── upstream/resolver.go        # 推理服务 → 实例 endpoint 解析（TTL 缓存、合并回源、健康摘流）
    ├── stream/proxy.go             # SSE 流式代理（逐块 Flush，断连取消上游）
    ├── stream/addrpolicy.go        # 实例地址限制（拨号时检查）
    ├── health/health.go            # /healthz、/readyz
    └── router/router.go            # 路由表 + 中间件链 + 路由类别
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
    C[Client] --> OB[Observe\n按路由类别计数与计时]
    OB --> R[Recovery]
    R -->|/healthz /readyz /metrics| H[Probe / Metrics]
    R -->|/api/v1/| CO[Correlation]
    CO --> TC[TraceContext\n沿用或生成 traceparent]
    TC --> AL[AccessLog]
    AL --> AZ[Authenticate\n校验 API key 并覆写租户头]
    AZ -->|401 / 403| C
    AZ --> RL[RateLimit\n已验证租户令牌桶]
    RL -->|429| C
    RL --> BL[LimitRequestBody\n请求体上限]
    BL -->|413| C
    BL --> RT{Route}
    RT -->|/api/v1/resources 等| UP[ReverseProxy]
    RT -->|/api/v1/namespaces/{ns}/inference/{svc}/chat 等| SP[SSE StreamProxy]
    UP --> CP[(Python 控制平面)]
    SP -->|解析 status.endpoints| CP
    SP -->|按 protocol 映射路径，只连接允许的地址| INS[(推理实例)]
```

---

## 8. 后续演进（首期骨架之外）

- 在 API key 之外接入 OIDC/JWT 与 mTLS（新的 `Authenticator` 实现）；key 文件热加载与过期时间（见 ADR-014 后续事项）。
- RateLimit 改为分布式限流（如 Redis），支持多网关实例共享配额。
- 协议/版本转换（`v1alpha1` ↔ 对外版本）与 gRPC-stream 转发。
- OpenTelemetry：记录 span 并以 OTLP 导出（现在只传递 `traceparent`，见「可观测性」）。
- `/metrics` 改到单独的监听地址，与对外入口分开。