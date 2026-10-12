package middleware

import (
	"fmt"
	"net/http"
)

// LimitRequestBody 限制请求体大小。声明的 Content-Length 超过 limit 时直接返回 413；
// 未声明长度（分块传输）的请求体读到超出 limit 的部分时返回 *http.MaxBytesError，
// 由转发处理器据此返回 413。没有请求体的请求原样放行。
func LimitRequestBody(limit int64) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.ContentLength > limit {
				WriteError(w, r, http.StatusRequestEntityTooLarge, "REQUEST_TOO_LARGE",
					fmt.Sprintf("request body exceeds %d bytes", limit))
				return
			}
			if r.Body != nil && r.Body != http.NoBody {
				r.Body = http.MaxBytesReader(w, r.Body, limit)
			}
			next.ServeHTTP(w, r)
		})
	}
}
