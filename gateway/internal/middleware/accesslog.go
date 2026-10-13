package middleware

import (
	"log"
	"net/http"
	"time"
)

// AccessLog 返回为每个请求输出一行访问日志的中间件，须位于 Correlation 与 TraceContext 之后，
// 使关联 ID 与 trace-id 出现在同一行：
//
//	access method=POST path="/api/v1/inference/svc/chat" status=200 duration=1.5ms corr=… trace=…
//
// path 不含查询串；流式请求的 duration 是整个流的时长。处理器 panic 时（由外层 Recovery
// 兜底）记录 status=500，响应头已写出时记录已写出的状态码。
func AccessLog(logger *log.Logger) Middleware {
	if logger == nil {
		logger = log.Default()
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			sw := &statusWriter{ResponseWriter: w}
			returned := false
			defer func() {
				logger.Printf("access method=%s path=%q status=%d duration=%s corr=%s trace=%s",
					r.Method, r.URL.Path, sw.statusAfter(returned), time.Since(start).Round(time.Microsecond),
					CorrelationIDFromContext(r.Context()), TraceIDFromContext(r.Context()))
			}()
			next.ServeHTTP(sw, r)
			returned = true
		})
	}
}
