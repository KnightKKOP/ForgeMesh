// Package router 组装网关的路由表与中间件链。
//
// 路由表（对外统一入口，版本前缀 /api/v1/）：
//
//	GET  /healthz                              存活探针（无中间件）
//	GET  /readyz                               就绪探针（探测上游可达，无中间件）
//	ANY  /api/v1/resources[/...]               反代到 Python 控制平面
//	ANY  /api/v1/inference-services[/...]      反代到 Python 控制平面
//	ANY  /api/v1/workflows[/...]               反代到 Python 控制平面
//	ANY  /api/v1/decisions[/...]               反代到 Python 控制平面
//	ANY  /api/v1/inference/{svc}/chat          SSE 流式代理
//
// 中间件链（执行顺序）：Recovery → Correlation → Authz → RateLimit → Route。
// Recovery 作为最外层兜底全量请求；探针刻意不经过鉴权/限流，保证健康检查可用。
package router

import (
	"fmt"
	"log"
	"net/http"
	"net/url"

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
}

// streamRoutePattern 是流式（SSE）代理路由，{svc} 为推理服务标识。
const streamRoutePattern = "/api/v1/inference/{svc}/chat"

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

	hc := health.New(cfg.UpstreamURL)
	proxy := upstream.New(target, cfg.CorrelationHeader, cfg.TenantHeader, logger)
	streamProxy := stream.New(target, logger)

	// 精细路由仅在 API 前缀内匹配（顶层已按 /api/v1/ 分流）。
	apiMux := http.NewServeMux()
	for _, p := range proxyRoutes {
		apiMux.Handle(p, proxy)     // 精确匹配集合端点
		apiMux.Handle(p+"/", proxy) // 匹配子资源
	}
	apiMux.Handle(streamRoutePattern, streamProxy)

	// API 中间件链：Correlation → Authz → RateLimit。
	limiter := middleware.NewRateLimiter(cfg.RateLimitRPS, cfg.RateLimitBurst)
	apiHandler := middleware.Chain(apiMux,
		middleware.Correlation(cfg.CorrelationHeader),
		middleware.Authz(cfg.TenantHeader),
		middleware.RateLimit(limiter, cfg.TenantHeader),
	)

	// 顶层路由：探针不经过鉴权/限流。
	root := http.NewServeMux()
	root.HandleFunc("/healthz", hc.Live)
	root.HandleFunc("/readyz", hc.Ready)
	root.Handle(apiPrefix, apiHandler)

	// Recovery 作为最外层，兜底全量请求的 panic。
	return &Router{handler: middleware.Recovery(logger)(root)}, nil
}

// ServeHTTP 实现 http.Handler。
func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	r.handler.ServeHTTP(w, req)
}

// Handler 返回底层 http.Handler，便于测试或上层复用。
func (r *Router) Handler() http.Handler { return r.handler }
