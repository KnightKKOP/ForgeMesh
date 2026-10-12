// resolver.go 实现推理服务 → 实例 endpoint 列表的解析。
//
// 数据源：控制平面 GET /api/v1/inference-services/{svc}[?namespace={ns}]（携带
// X-Tenant-Id 与调用方的 Authorization，控制平面据此独立校验调用方，见
// WithAuthorization），从响应 status.endpoints[] 取就绪实例的地址与协议
// （如 {"url": "http://10.0.0.1:8000", "protocol": "openai-chat"}）。
//
// 设计要点：
//   - 内存缓存：service→(endpoints, expiry)，TTL 内复用，减少控制平面压力；
//     缓存 key 含租户 ID 与命名空间，保证多租户、多命名空间的同名服务互相隔离。
//   - 合并回源：同一个 key 的并发未命中共用一次控制平面查询，缓存过期的瞬间
//     突发请求不会同时打到控制平面；"服务不存在 / 未就绪 / 无可用实例"缓存 1 秒。
//   - 只返回 ready 且协议已知的实例；协议决定实例上的推理路径（见 Protocol.Path）。
//   - 健康摘流：每个 endpoint 维护连续失败计数，达到阈值（3 次）即标记
//     unhealthy 进入冷却期；冷却结束后按 half-open 语义重新放行（计数清零，
//     给实例一次探测机会，避免"一次失败即永久摘除"或"反复抖动"）。
//   - 解析失败（控制平面 404 / 网络错误 / status.endpoints 为空 /
//     phase 非 Ready）返回哨兵错误，调用方据此生成 502/503；控制平面 403
//     返回 ErrForbidden，调用方据此生成 403。
//
// 依赖约束：仅使用标准库（encoding/json 等），与 config 包约束一致。
package upstream

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// 解析失败的哨兵错误，调用方通过 errors.Is 分类转成对应的 HTTP 状态码。
var (
	// ErrServiceNotFound 控制平面返回 404：推理服务不存在。
	ErrServiceNotFound = errors.New("inference service not found")
	// ErrForbidden 控制平面返回 403：调用方凭证无权读取该推理服务。
	ErrForbidden = errors.New("credentials may not read the inference service")
	// ErrNotReady 资源 status.phase 不为 Ready：服务尚未就绪。
	ErrNotReady = errors.New("inference service not Ready")
	// ErrNoEndpoints status.endpoints 中没有就绪、协议已知且地址合法的实例。
	ErrNoEndpoints = errors.New("inference service has no usable endpoints")
	// ErrNoHealthyEndpoints 注册的实例全部处于不健康冷却期。
	ErrNoHealthyEndpoints = errors.New("no healthy endpoints available")
)

// 调参默认值。
const (
	// DefaultResolverTTL 是服务→实例列表缓存默认有效期。
	DefaultResolverTTL = 5 * time.Second
	// DefaultUnhealthyCooldown 是实例被摘流后的默认冷却时长。
	DefaultUnhealthyCooldown = 10 * time.Second
	// defaultFailureThreshold 是连续失败多少次后摘流。
	defaultFailureThreshold = 3
	// resolveHTTPTimeout 是查询控制平面的单次 HTTP 超时。
	resolveHTTPTimeout = 5 * time.Second
	// negativeCacheTTL 是否定结果的缓存时长：吸收同一时刻的重试，又不让刚就绪的
	// 服务长时间不可用。
	negativeCacheTTL = time.Second
	// maxResolveBodySize 限制控制平面响应体读取量，防御异常大响应。
	maxResolveBodySize = 1 << 20 // 1 MiB
)

// ServiceRef 标识一个推理服务。Namespace 为空表示不带命名空间的服务，与控制平面
// GET /api/v1/inference-services/{name} 不带 namespace 参数时的语义一致。
type ServiceRef struct {
	Namespace string
	Name      string
}

// String 返回 "namespace/name"，不带命名空间时只有 name。
func (s ServiceRef) String() string {
	if s.Namespace == "" {
		return s.Name
	}
	return s.Namespace + "/" + s.Name
}

// Protocol 是推理实例提供的协议（对齐资源模型 EndpointProtocol）。
type Protocol string

// 已知的实例协议。
const (
	ProtocolOpenAIChat  Protocol = "openai-chat"
	ProtocolTGIGenerate Protocol = "tgi-generate"
)

// protocolPaths 是各协议在实例上的推理路径。
var protocolPaths = map[Protocol]string{
	ProtocolOpenAIChat:  "/v1/chat/completions",
	ProtocolTGIGenerate: "/generate_stream",
}

// Path 返回协议在实例上的推理路径；未知协议返回 false。
func (p Protocol) Path() (string, bool) {
	path, ok := protocolPaths[p]
	return path, ok
}

// Endpoint 是一个可以转发推理请求的实例。
type Endpoint struct {
	URL      string
	Protocol Protocol
}

// endpointPayload 对应资源模型的 Endpoint；ready 缺省视为就绪（与资源模型默认值一致）。
type endpointPayload struct {
	URL      string   `json:"url"`
	Ready    *bool    `json:"ready"`
	Protocol Protocol `json:"protocol"`
}

// inferenceServicePayload 是控制平面响应的最小解析结构（camelCase 对齐资源 JSON）。
// 只声明路由关心的字段，字段缺失（旧版本控制平面）时取零值：
// phase="" 视为非 Ready、endpoints=nil 视为无实例。
type inferenceServicePayload struct {
	Status struct {
		Phase     string            `json:"phase"`
		Endpoints []endpointPayload `json:"endpoints"`
	} `json:"status"`
}

type authorizationKey struct{}

// WithAuthorization 返回携带调用方 Authorization 请求头的 context。Resolver 查询控制平面
// 时原样附带，控制平面用同一份凭证独立校验调用方与租户；value 为空时原样返回 ctx。
func WithAuthorization(ctx context.Context, value string) context.Context {
	if value == "" {
		return ctx
	}
	return context.WithValue(ctx, authorizationKey{}, value)
}

// cacheEntry 是服务级缓存条目；err 非空时缓存的是否定结果（见 isCacheableMiss）。
type cacheEntry struct {
	endpoints []Endpoint
	err       error
	expiry    time.Time
}

// lookup 是一次进行中的控制平面查询，同一个缓存 key 的并发未命中共用它；
// done 关闭之后 endpoints / err 只读。查询使用发起者的凭证，等待者共用结果与共用
// 缓存的语义相同：网关已对每个调用方校验过凭证、租户与 read scope。
type lookup struct {
	done      chan struct{}
	endpoints []Endpoint
	err       error
}

// endpointHealth 是实例级健康状态（受 Resolver.mu 保护）。
type endpointHealth struct {
	consecutiveFailures int
	unhealthyUntil      time.Time // 零值表示未被摘流
}

// Resolver 将推理服务名解析为可用的实例 endpoint 列表（实现 stream.Resolver 接口）。
type Resolver struct {
	controlPlaneURL  string
	client           *http.Client
	ttl              time.Duration
	negativeTTL      time.Duration
	cooldown         time.Duration
	failureThreshold int
	logger           *log.Logger

	mu       sync.Mutex
	cache    map[string]cacheEntry      // key: 见 cacheKey
	inflight map[string]*lookup         // key: 见 cacheKey
	health   map[string]*endpointHealth // key: endpoint URL 字符串
}

// NewResolver 构造解析器。
//
// controlPlaneURL 为控制平面基地址（如 "http://control-plane:8080"）；
// client 为 nil 时使用带 5s 超时、连接池放宽的默认客户端；ttl / cooldown 非正数时
// 使用 DefaultResolverTTL / DefaultUnhealthyCooldown；logger 为 nil 时用 log.Default()。
func NewResolver(controlPlaneURL string, client *http.Client, ttl, cooldown time.Duration, logger *log.Logger) *Resolver {
	if client == nil {
		client = &http.Client{Timeout: resolveHTTPTimeout, Transport: newControlPlaneTransport()}
	}
	if ttl <= 0 {
		ttl = DefaultResolverTTL
	}
	if cooldown <= 0 {
		cooldown = DefaultUnhealthyCooldown
	}
	if logger == nil {
		logger = log.Default()
	}
	return &Resolver{
		controlPlaneURL:  strings.TrimRight(controlPlaneURL, "/"),
		client:           client,
		ttl:              ttl,
		negativeTTL:      min(ttl, negativeCacheTTL),
		cooldown:         cooldown,
		failureThreshold: defaultFailureThreshold,
		logger:           logger,
		cache:            make(map[string]cacheEntry),
		inflight:         make(map[string]*lookup),
		health:           make(map[string]*endpointHealth),
	}
}

// Resolve 返回服务的健康实例 endpoint 列表（已过滤冷却期内的实例）。
//
// 返回哨兵错误：ErrServiceNotFound / ErrNotReady / ErrNoEndpoints /
// ErrNoHealthyEndpoints；网络错误与控制平面异常返回包装后的普通错误；
// ctx 在查询完成前结束时返回 ctx.Err()。
func (r *Resolver) Resolve(ctx context.Context, svc ServiceRef, tenantID string) ([]Endpoint, error) {
	key := cacheKey(tenantID, svc)

	r.mu.Lock()
	if entry, ok := r.cache[key]; ok && time.Now().Before(entry.expiry) {
		defer r.mu.Unlock()
		return r.healthyLocked(entry.endpoints, entry.err, svc, tenantID)
	}
	l, running := r.inflight[key]
	if !running {
		l = &lookup{done: make(chan struct{})}
		r.inflight[key] = l
		// 查询不随发起它的调用方取消：其他等待者与缓存还需要结果。
		go r.runLookup(context.WithoutCancel(ctx), key, svc, tenantID, l)
	}
	r.mu.Unlock()

	select {
	case <-l.done:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.healthyLocked(l.endpoints, l.err, svc, tenantID)
}

// runLookup 查询控制平面，写入缓存后唤醒全部等待者。成功结果缓存 ttl；与凭证无关的
// 否定结果缓存 negativeTTL；其余失败不缓存，下一个请求立即重试。
func (r *Resolver) runLookup(ctx context.Context, key string, svc ServiceRef, tenantID string, l *lookup) {
	ctx, cancel := context.WithTimeout(ctx, resolveHTTPTimeout)
	defer cancel()
	endpoints, err := r.fetch(ctx, svc, tenantID)

	r.mu.Lock()
	switch {
	case err == nil:
		r.cache[key] = cacheEntry{endpoints: endpoints, expiry: time.Now().Add(r.ttl)}
	case isCacheableMiss(err):
		r.cache[key] = cacheEntry{err: err, expiry: time.Now().Add(r.negativeTTL)}
	}
	delete(r.inflight, key)
	l.endpoints, l.err = endpoints, err
	r.mu.Unlock()
	close(l.done)
}

// healthyLocked 返回解析结果中不在冷却期的实例（调用方须持有 r.mu）。
func (r *Resolver) healthyLocked(endpoints []Endpoint, err error, svc ServiceRef, tenantID string) ([]Endpoint, error) {
	if err != nil {
		return nil, err
	}
	healthy := r.filterHealthyLocked(endpoints)
	if len(healthy) == 0 {
		return nil, fmt.Errorf("%w: service=%s tenant=%s", ErrNoHealthyEndpoints, svc, tenantID)
	}
	return healthy, nil
}

// isCacheableMiss 报告 err 是否为可以短暂缓存的否定结果。它们只取决于租户与服务，
// 与调用方凭证无关；ErrForbidden 取决于凭证，网络错误与控制平面异常是暂时的，都不缓存。
func isCacheableMiss(err error) bool {
	return errors.Is(err, ErrServiceNotFound) || errors.Is(err, ErrNotReady) ||
		errors.Is(err, ErrNoEndpoints)
}

// fetch 调用控制平面 GET /api/v1/inference-services/{svc}[?namespace={ns}] 并提取 endpoints。
func (r *Resolver) fetch(ctx context.Context, svc ServiceRef, tenantID string) ([]Endpoint, error) {
	reqURL := r.controlPlaneURL + "/api/v1/inference-services/" + url.PathEscape(svc.Name)
	if svc.Namespace != "" {
		reqURL += "?" + url.Values{"namespace": {svc.Namespace}}.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, fmt.Errorf("resolver: 构造请求失败: %w", err)
	}
	// 租户已由网关认证中间件取自凭证；控制平面会核对它与凭证绑定的租户一致。
	req.Header.Set("X-Tenant-Id", tenantID)
	req.Header.Set("Accept", "application/json")
	if auth, _ := ctx.Value(authorizationKey{}).(string); auth != "" {
		req.Header.Set("Authorization", auth)
	}

	resp, err := r.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("resolver: 查询控制平面失败 (svc=%s): %w", svc, err)
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode == http.StatusNotFound:
		return nil, fmt.Errorf("%w: svc=%s tenant=%s", ErrServiceNotFound, svc, tenantID)
	case resp.StatusCode == http.StatusForbidden:
		return nil, fmt.Errorf("%w: svc=%s tenant=%s", ErrForbidden, svc, tenantID)
	case resp.StatusCode < 200 || resp.StatusCode >= 300:
		return nil, fmt.Errorf("resolver: 控制平面返回异常状态 %d (svc=%s)", resp.StatusCode, svc)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResolveBodySize))
	if err != nil {
		return nil, fmt.Errorf("resolver: 读取控制平面响应失败 (svc=%s): %w", svc, err)
	}

	var payload inferenceServicePayload
	if err := json.Unmarshal(body, &payload); err != nil {
		return nil, fmt.Errorf("resolver: 解析控制平面响应失败 (svc=%s): %w", svc, err)
	}

	if !strings.EqualFold(payload.Status.Phase, "Ready") {
		return nil, fmt.Errorf("%w: svc=%s phase=%q", ErrNotReady, svc, payload.Status.Phase)
	}

	usable := make([]Endpoint, 0, len(payload.Status.Endpoints))
	for _, ep := range payload.Status.Endpoints {
		if ep.Ready != nil && !*ep.Ready {
			continue
		}
		if _, known := ep.Protocol.Path(); !known {
			r.logger.Printf("resolver: 跳过协议未知的 endpoint %q protocol=%q (svc=%s)", ep.URL, ep.Protocol, svc)
			continue
		}
		if u, err := url.Parse(ep.URL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			r.logger.Printf("resolver: 跳过非法 endpoint %q (svc=%s)", ep.URL, svc)
			continue
		}
		usable = append(usable, Endpoint{URL: ep.URL, Protocol: ep.Protocol})
	}
	if len(usable) == 0 {
		return nil, fmt.Errorf("%w: svc=%s", ErrNoEndpoints, svc)
	}
	return usable, nil
}

// ReportFailure 记录一次实例转发失败；连续失败达到阈值后摘流进入冷却期。
func (r *Resolver) ReportFailure(endpoint string) {
	r.mu.Lock()
	defer r.mu.Unlock()

	h, ok := r.health[endpoint]
	if !ok {
		h = &endpointHealth{}
		r.health[endpoint] = h
	}
	h.consecutiveFailures++
	if h.consecutiveFailures >= r.failureThreshold {
		h.unhealthyUntil = time.Now().Add(r.cooldown)
		r.logger.Printf("resolver: endpoint %s 连续失败 %d 次，摘流 %s",
			endpoint, h.consecutiveFailures, r.cooldown)
	}
}

// ReportSuccess 记录一次实例转发成功，清零失败计数并解除摘流状态。
func (r *Resolver) ReportSuccess(endpoint string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.health, endpoint)
}

// filterHealthyLocked 过滤冷却期内的 endpoint（调用方须持有 r.mu）。
//
// 冷却期结束的实例按 half-open 语义放行：清零失败计数，给实例一次
// 重新证明健康的机会；若再连续失败会再次进入冷却。
func (r *Resolver) filterHealthyLocked(endpoints []Endpoint) []Endpoint {
	now := time.Now()
	healthy := make([]Endpoint, 0, len(endpoints))
	for _, ep := range endpoints {
		h, ok := r.health[ep.URL]
		if !ok {
			healthy = append(healthy, ep)
			continue
		}
		if !h.unhealthyUntil.IsZero() {
			if now.Before(h.unhealthyUntil) {
				continue // 仍在冷却期，摘流。
			}
			// 冷却结束：half-open 放行并清零计数。
			h.consecutiveFailures = 0
			h.unhealthyUntil = time.Time{}
		}
		healthy = append(healthy, ep)
	}
	return healthy
}

// cacheKey 生成按租户与命名空间隔离的缓存 key（"\x00" 不会出现在合法的名字中）。
func cacheKey(tenantID string, svc ServiceRef) string {
	return tenantID + "\x00" + svc.Namespace + "\x00" + svc.Name
}
