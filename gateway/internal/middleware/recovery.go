package middleware

import (
	"context"
	"log"
	"net/http"
	"runtime/debug"
)

// requestIDs 记录 Correlation 与 TraceContext 为请求确定的 ID，只在处理请求的 goroutine 中读写。
type requestIDs struct {
	correlationID string
	traceID       string
}

type requestIDsKey struct{}

// requestIDsFrom 返回 Recovery 放入 context 的 requestIDs；链上没有 Recovery 时返回 nil。
func requestIDsFrom(ctx context.Context) *requestIDs {
	ids, _ := ctx.Value(requestIDsKey{}).(*requestIDs)
	return ids
}

// Recovery 返回兜底 panic 的中间件：记录关联 ID、trace-id、方法、路径与堆栈，响应头尚未写出时
// 返回 500 INTERNAL_ERROR 错误信封（不含 panic 的内容）。
//
// Recovery 位于 Correlation 与 TraceContext 之外，读不到它们写入下游 context 的 ID，改由它们
// 记录到 Recovery 放入 context 的 requestIDs；没有经过这两个中间件的请求（探针与 /metrics）
// 日志与信封中没有 ID。
//
// 响应头已写出时，状态码与已写出的响应体（可能来自控制平面或推理实例）都无法更改：记录日志后
// 以 http.ErrAbortHandler 中止响应，客户端看到的是不完整的响应，而不是末尾追加的错误信封。
// panic(http.ErrAbortHandler) 本身（如 httputil.ReverseProxy 复制响应体失败）不记录日志，原样
// 交给 net/http。
func Recovery(logger *log.Logger) Middleware {
	if logger == nil {
		logger = log.Default()
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ids := &requestIDs{}
			sw := &statusWriter{ResponseWriter: w}
			defer func() {
				rec := recover()
				if rec == nil {
					return
				}
				if rec == http.ErrAbortHandler {
					panic(rec)
				}
				logger.Printf("panic recovered: corr=%s trace=%s method=%s path=%s err=%v\n%s",
					ids.correlationID, ids.traceID, r.Method, r.URL.Path, rec, debug.Stack())
				if sw.status != 0 {
					panic(http.ErrAbortHandler)
				}
				writeEnvelope(w, http.StatusInternalServerError, "INTERNAL_ERROR", "internal server error",
					ids.correlationID, ids.traceID)
			}()
			next.ServeHTTP(sw, r.WithContext(context.WithValue(r.Context(), requestIDsKey{}, ids)))
		})
	}
}
