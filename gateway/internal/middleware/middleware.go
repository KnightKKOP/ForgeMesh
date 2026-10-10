// Package middleware 提供网关统一的中间件链。
//
// 链的执行顺序（外层 → 内层）：
//
//	Recovery → Correlation → RateLimit → Authz → Route
//
// Recovery 位于最外层，保证任何下游 panic 都能被兜底；
// Correlation 尽早生成关联 ID，便于后续日志与限流观测。
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
