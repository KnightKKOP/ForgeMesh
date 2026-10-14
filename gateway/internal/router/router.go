// Package router 组装网关的路由表与中间件链。
//
// 路由表（对外统一入口，版本前缀 /api/v1/）：
//
//	GET  /healthz                              存活探针（无中间件）
//	GET  /readyz                               就绪探针（请求控制平面 /readyz，无中间件）
//	ANY  /api/v1/resources[/...]               反代到 Python 控制平面
//	ANY  /api/v1/inference-services[/...]      反代到 Python 控制平面
//	ANY  /api/v1/workflows[/...]               反代到 Python 控制平面
//	ANY  /api/v1/decisions[/...]               反代到 Python 控制平面
//	ANY  /api/v1/audit-events[/...]            反代到 Python 控制平面
//	ANY  /api/v1/namespaces/{ns}/inference/{svc}/chat
//	                                          SSE 流式代理（服务经 Resolver 解析为
//	                                          ready 实例，轮询分发，按实例协议映射
//	                                          路径；要求 read 作用域）
//	ANY  /api/v1/inference/{svc}/chat          同上，用于不带命名空间的服务
//
// 中间件链（执行顺序）：Recovery → Correlation → Authenticate → RateLimit →
// LimitRequestBody → Route。
// Recovery 作为最外层兜底全量请求；探针刻意不经过认证/限流，保证健康检查可用。
// 认证模式见 config.AuthModeAPIKey / config.AuthModeDev 与 ADR-014。
package router

import (
	"fmt"
	"log"
	"net/http"
	"net/url"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/health"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/middleware"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/stream"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/upstream"
)

// apiPrefix 是所有对外 API 的版本前缀。
const apiPrefix = "/api/v1/"

// proxyRoutes 是需要反代到 Python 控制平面的资源类路由前缀。
var proxyRoutes = []string{
	"/api/v1/resources",
	"/api/v1/inference-services",
	"/api/v1/workflows",
	"/api/v1/decisions",
	"/api/v1/audit-events",
}

// 流式（SSE）代理路由：{ns} 为命名空间，{svc} 为推理服务名。
const (
	streamRoutePattern            = "/api/v1/namespaces/{ns}/inference/{svc}/chat"
	streamWithoutNamespacePattern = "/api/v1/inference/{svc}/chat"
)

// Router 组装路由表与中间件链，实现 http.Handler。
type Router struct {
	handler http.Handler
}

// New 根据配置构造网关处理器。logger 为 nil 时使用 log.Default()。
func New(cfg config.Config, logger *log.Logger) (*Router, error) {
	if logger == nil {
		logger = log.Default()
	}

	target, err := url.Parse(cfg.UpstreamURL)
	if err != nil {
		return nil, fmt.Errorf("router: 解析上游地址 %q 失败: %w", cfg.UpstreamURL, err)
	}
	if target.Scheme == "" || target.Host == "" {
		return nil, fmt.Errorf("router: 上游地址必须包含 scheme 与 host: %q", cfg.UpstreamURL)
	}
	authn, err := newAuthenticator(cfg)
	if err != nil {
		return nil, err
	}

	hc := health.New(cfg.UpstreamURL)
	proxy := upstream.New(target, cfg.CorrelationHeader, cfg.TenantHeader, logger)

	// 推理路由解析器：以控制平面为数据源，把 {svc} 解析为实例 endpoint 列表。
	// 零值/负值配置回退默认，保持对旧配置的向后兼容。
	resolverTTL := time.Duration(cfg.ResolverTTLSeconds) * time.Second
	if resolverTTL <= 0 {
		resolverTTL = upstream.DefaultResolverTTL
	}
	unhealthyCooldown := time.Duration(cfg.UnhealthyCooldownSeconds) * time.Second
	if unhealthyCooldown <= 0 {
		unhealthyCooldown = upstream.DefaultUnhealthyCooldown
	}
	resolver := upstream.NewResolver(cfg.UpstreamURL, nil, resolverTTL, unhealthyCooldown, logger)
	streamProxy := stream.New(resolver, cfg.TenantHeader, stream.NewAddressPolicy(cfg.InstanceAllowedCIDRs), logger)

	// 精细路由仅在 API 前缀内匹配（顶层已按 /api/v1/ 分流）。
	apiMux := http.NewServeMux()
	for _, p := range proxyRoutes {
		apiMux.Handle(p, proxy)     // 精确匹配集合端点
		apiMux.Handle(p+"/", proxy) // 匹配子资源
	}
	// 推理调用读取服务的 status.endpoints 并调用实例，不修改资源，故要求 read 作用域；
	// 在网关侧检查，使命中 Resolver 缓存的请求同样受作用域约束。
	streamHandler := middleware.RequireScope(middleware.ScopeRead)(streamProxy)
	apiMux.Handle(streamRoutePattern, streamHandler)
	apiMux.Handle(streamWithoutNamespacePattern, streamHandler)

	maxBody := cfg.MaxRequestBodyBytes
	if maxBody <= 0 {
		maxBody = config.DefaultMaxRequestBodyBytes
	}

	// API 中间件链：Correlation → Authenticate → RateLimit → LimitRequestBody。
	limiter := middleware.NewRateLimiter(cfg.RateLimitRPS, cfg.RateLimitBurst)
	apiHandler := middleware.Chain(apiMux,
		middleware.Correlation(cfg.CorrelationHeader),
		middleware.Authenticate(authn, cfg.TenantHeader),
		middleware.RateLimit(limiter),
		middleware.LimitRequestBody(maxBody),
	)

	// 顶层路由：探针不经过鉴权/限流。
	root := http.NewServeMux()
	root.HandleFunc("/healthz", hc.Live)
	root.HandleFunc("/readyz", hc.Ready)
	root.Handle(apiPrefix, apiHandler)

	// Recovery 作为最外层，兜底全量请求的 panic。
	return &Router{handler: middleware.Recovery(logger)(root)}, nil
}

// newAuthenticator 按配置构造入口认证器；未知模式视为配置错误，绝不退化为信任请求头。
func newAuthenticator(cfg config.Config) (middleware.Authenticator, error) {
	switch cfg.AuthMode {
	case config.AuthModeAPIKey:
		keys, err := middleware.LoadAPIKeys(cfg.APIKeysFile)
		if err != nil {
			return nil, fmt.Errorf("router: %w", err)
		}
		authn, err := middleware.NewAPIKeyAuthenticator(keys, cfg.TenantHeader)
		if err != nil {
			return nil, fmt.Errorf("router: %w", err)
		}
		return authn, nil
	case config.AuthModeDev:
		return middleware.HeaderAuthenticator{TenantHeader: cfg.TenantHeader}, nil
	default:
		return nil, fmt.Errorf("router: 未知认证模式 %q（可选 %q、%q）",
			cfg.AuthMode, config.AuthModeAPIKey, config.AuthModeDev)
	}
}

// ServeHTTP 实现 http.Handler。
func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	r.handler.ServeHTTP(w, req)
}

// Handler 返回底层 http.Handler，便于测试或上层复用。
func (r *Router) Handler() http.Handler { return r.handler }
