package middleware

import (
	"log"
	"net/http"
	"runtime/debug"
)

// Recovery 返回兜底 panic 的中间件。
//
// 行为：捕获下游 handler 抛出的 panic，记录关联 ID、路径与堆栈，
// 并返回 500，避免单个请求的 panic 导致整个网关进程退出。
//
// 注意：若响应头/状态码已被写出，则无法再改为 500，此时仅记录日志。
func Recovery(logger *log.Logger) Middleware {
	if logger == nil {
		logger = log.Default()
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if rec := recover(); rec != nil {
					logger.Printf(
						"panic recovered: corr=%s method=%s path=%s err=%v\n%s",
						CorrelationIDFromContext(r.Context()),
						r.Method,
						r.URL.Path,
						rec,
						debug.Stack(),
					)
					http.Error(w, http.StatusText(http.StatusInternalServerError), http.StatusInternalServerError)
				}
			}()
			next.ServeHTTP(w, r)
		})
	}
}