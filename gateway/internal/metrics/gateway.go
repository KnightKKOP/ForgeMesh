package metrics

import (
	"net/http"
	"strconv"
	"time"
)

// Route 是请求的路由类别（http_requests_total 等指标的 route 标签）。
type Route string

// 路由类别，由 router 按路由表确定。
const (
	RouteProbe        Route = "probe"         // /healthz、/readyz
	RouteMetrics      Route = "metrics"       // /metrics
	RouteControlPlane Route = "control_plane" // 反代到控制平面的资源接口
	RouteInference    Route = "inference"     // 流式推理代理
	RouteUnmatched    Route = "unmatched"     // 没有匹配的路由（含 /api/v1/ 下的未知路径）
)

// Upstream 是出错的上游（upstream_errors_total 的 upstream 标签）。
type Upstream string

// 网关的上游。
const (
	UpstreamControlPlane Upstream = "control_plane" // 资源接口反代与 Resolver 的查询
	UpstreamInstance     Upstream = "instance"      // 推理实例
)

// ErrorKind 是上游错误的类别（upstream_errors_total 的 kind 标签）。
type ErrorKind string

// 上游错误类别。
const (
	// ErrorConnect 没有收到响应：连接失败、超时等（客户端已断开的请求不计）。
	ErrorConnect ErrorKind = "connect"
	// ErrorStatus 上游返回 5xx。
	ErrorStatus ErrorKind = "status"
	// ErrorBody 响应体读取或解析失败：流在中途中断，或控制平面响应不是合法 JSON。
	ErrorBody ErrorKind = "body"
	// ErrorAddressNotAllowed 实例地址不在允许连接的范围内。
	ErrorAddressNotAllowed ErrorKind = "address_not_allowed"
	// ErrorInvalidEndpoint 实例地址或协议非法。
	ErrorInvalidEndpoint ErrorKind = "invalid_endpoint"
)

// LookupResult 是 Resolver 一次查询的结果（resolver_lookups_total 的 result 标签）。
type LookupResult string

// Resolver 查询结果。
const (
	LookupHit       LookupResult = "hit"       // 缓存命中（含缓存的否定结果）
	LookupMiss      LookupResult = "miss"      // 未命中，向控制平面查询
	LookupCoalesced LookupResult = "coalesced" // 未命中，并入同一服务进行中的查询
)

// requestBuckets 覆盖短请求与长时间的流式请求（秒）。
var requestBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300}

// firstByteBuckets 覆盖推理首个 token 的常见时延（秒）。
var firstByteBuckets = []float64{0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60}

// Gateway 是网关的全部指标，实现 http.Handler（/metrics）。指标如下，标签都不含租户、
// 服务名、路径或实例地址：
//
//   - forgemesh_gateway_http_requests_total{route,status}：处理的 HTTP 请求数，route 为路由类别，
//     status 为响应状态码。
//   - forgemesh_gateway_http_request_duration_seconds{route}：请求耗时；流式请求为整个流的时长。
//   - forgemesh_gateway_stream_first_byte_seconds：流式代理从开始处理请求（含解析实例）到把实例的
//     第一个响应体字节写给客户端的时间，只统计实例返回 2xx 的流。
//   - forgemesh_gateway_upstream_errors_total{upstream,kind}：上游错误数，见 Upstream 与 ErrorKind。
//   - forgemesh_gateway_rate_limited_total：被限流拒绝（429）的请求数。
//   - forgemesh_gateway_resolver_lookups_total{result}：Resolver 的查询数，见 LookupResult。
//   - forgemesh_gateway_instances{state}：Resolver 缓存中（TTL 内解析过）的推理实例数：healthy
//     参与轮询，unhealthy 因连续失败处于冷却期、被摘流。
//
// nil 的 *Gateway 上的记录方法什么都不做，组件可以不配置指标。
type Gateway struct {
	registry       *Registry
	requests       *Counter
	duration       *Histogram
	firstByte      *Histogram
	upstreamErrors *Counter
	lookups        *Counter
}

// NewGateway 创建网关指标。
func NewGateway() *Gateway {
	r := NewRegistry()
	g := &Gateway{
		registry: r,
		requests: r.NewCounter("forgemesh_gateway_http_requests_total",
			"HTTP requests handled by the gateway, by route class and status.", "route", "status"),
		duration: r.NewHistogram("forgemesh_gateway_http_request_duration_seconds",
			"Time to handle an HTTP request, by route class; streams count until they end.",
			requestBuckets, "route"),
		firstByte: r.NewHistogram("forgemesh_gateway_stream_first_byte_seconds",
			"Time from the start of a streaming request to its first response byte (2xx streams).",
			firstByteBuckets),
		upstreamErrors: r.NewCounter("forgemesh_gateway_upstream_errors_total",
			"Errors from the control plane and inference instances, by upstream and kind.",
			"upstream", "kind"),
		lookups: r.NewCounter("forgemesh_gateway_resolver_lookups_total",
			"Inference service resolutions, by result (hit, miss, coalesced).", "result"),
	}
	for _, result := range []LookupResult{LookupHit, LookupMiss, LookupCoalesced} {
		g.lookups.Add(0, string(result))
	}
	return g
}

// ObserveRequest 记录一个处理完的请求。
func (g *Gateway) ObserveRequest(route Route, status int, elapsed time.Duration) {
	if g == nil {
		return
	}
	g.requests.Inc(string(route), strconv.Itoa(status))
	g.duration.Observe(elapsed.Seconds(), string(route))
}

// ObserveFirstByte 记录一个流的首字节时间。
func (g *Gateway) ObserveFirstByte(elapsed time.Duration) {
	if g == nil {
		return
	}
	g.firstByte.Observe(elapsed.Seconds())
}

// UpstreamError 记录一次上游错误。
func (g *Gateway) UpstreamError(upstream Upstream, kind ErrorKind) {
	if g == nil {
		return
	}
	g.upstreamErrors.Inc(string(upstream), string(kind))
}

// ResolverLookup 记录一次 Resolver 查询。
func (g *Gateway) ResolverLookup(result LookupResult) {
	if g == nil {
		return
	}
	g.lookups.Inc(string(result))
}

// WatchRateLimiter 登记 forgemesh_gateway_rate_limited_total，取值为抓取时 rejected 的返回值
// （限流器累计拒绝的请求数）。
func (g *Gateway) WatchRateLimiter(rejected func() uint64) {
	g.registry.NewCounterFunc("forgemesh_gateway_rate_limited_total",
		"Requests rejected by the rate limiter (429).", nil,
		func() []Sample { return []Sample{{Value: float64(rejected())}} })
}

// WatchInstances 登记 forgemesh_gateway_instances，取值为抓取时 states 的返回值。
func (g *Gateway) WatchInstances(states func() (healthy, unhealthy int)) {
	g.registry.NewGaugeFunc("forgemesh_gateway_instances",
		"Inference instances in the resolver cache, by health state.", []string{"state"},
		func() []Sample {
			healthy, unhealthy := states()
			return []Sample{
				{Labels: []string{"healthy"}, Value: float64(healthy)},
				{Labels: []string{"unhealthy"}, Value: float64(unhealthy)},
			}
		})
}

// ServeHTTP 输出全部指标（见 Registry.ServeHTTP）。
func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	g.registry.ServeHTTP(w, r)
}
