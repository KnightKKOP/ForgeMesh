package middleware

import (
	"net/http"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics"
)

// Observe 返回记录请求数与耗时的中间件，须位于最外层（Recovery 之外），panic 兜底的 500
// 也被计入。classify 在请求开始时按路由表确定路由类别，指标只用路由类别、不用路径；
// m 为 nil 时不记录。流式请求的耗时是整个流的时长。
func Observe(m *metrics.Gateway, classify func(*http.Request) metrics.Route) Middleware {
	if classify == nil {
		panic("middleware: Observe 需要非空 classify")
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			route := classify(r)
			sw := &statusWriter{ResponseWriter: w}
			next.ServeHTTP(sw, r)
			m.ObserveRequest(route, sw.Status(), time.Since(start))
		})
	}
}

// statusWriter 记录写出的状态码。实现 http.Flusher（流式代理逐块 Flush）与 Unwrap
// （http.ResponseController 借此使用底层连接的其他能力）。
type statusWriter struct {
	http.ResponseWriter
	status int
}

// WriteHeader 记录第一个最终状态码：1xx 信息响应（101 除外）之后还有最终响应，不记录。
func (s *statusWriter) WriteHeader(code int) {
	if s.status == 0 && (code >= 200 || code == http.StatusSwitchingProtocols) {
		s.status = code
	}
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusWriter) Write(b []byte) (int, error) {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	return s.ResponseWriter.Write(b)
}

// Flush 把已写出的内容推给客户端；底层不支持 Flush 时什么都不做。
func (s *statusWriter) Flush() {
	if s.status == 0 {
		s.status = http.StatusOK
	}
	if f, ok := s.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Unwrap 返回底层的 ResponseWriter。
func (s *statusWriter) Unwrap() http.ResponseWriter { return s.ResponseWriter }

// Status 返回响应的状态码；处理器什么都没写时 net/http 回复 200。
func (s *statusWriter) Status() int {
	if s.status == 0 {
		return http.StatusOK
	}
	return s.status
}
