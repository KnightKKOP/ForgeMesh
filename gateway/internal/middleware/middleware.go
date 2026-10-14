// Package middleware 提供网关统一的中间件链。
//
// 链的执行顺序（外层 → 内层）：
//
//	Observe → Recovery → Correlation → TraceContext → AccessLog → Authenticate → RateLimit →
//	LimitRequestBody → Route
//
// Observe 位于最外层，按路由类别记录请求数与耗时（含 Recovery 兜底返回的 500）；
// Recovery 保证任何下游 panic 都能被兜底；
// Correlation 尽早生成关联 ID，认证失败的响应也能携带；
// TraceContext 沿用或生成 traceparent，AccessLog 在它之后，访问日志同时带关联 ID 与 trace-id，
// 被认证或限流拒绝的请求也有记录；
// Authenticate 先于 RateLimit，被拒请求不消耗令牌；
// LimitRequestBody 紧挨路由，请求体只在转发时才被读取。
package middleware

import "net/http"

// Middleware 是标准的 HTTP 中间件签名，对齐开发计划 §5.8 的 Middleware 定义。
type Middleware func(http.Handler) http.Handler

// Chain 按声明顺序包裹 handler。
//
// 语义：Chain(h, m1, m2, m3) 的实际执行顺序为 m1 → m2 → m3 → h，
// 即列表中越靠前的中间件越先处理请求。
func Chain(h http.Handler, mws ...Middleware) http.Handler {
	// 从右向左包裹：最后包裹的中间件位于最外层，最先执行。
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}
