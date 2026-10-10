package middleware

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"time"
)

// DefaultCorrelationHeader 是默认的链路关联 ID 请求头名称。
const DefaultCorrelationHeader = "X-Correlation-ID"

// contextKey 是本包私有的 context key 类型，避免与其他包的 key 冲突。
type contextKey int

const (
	correlationIDKey contextKey = iota
	tenantIDKey
)

// CorrelationIDFromContext 返回请求上下文中携带的关联 ID，缺失时返回空字符串。
func CorrelationIDFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	v, _ := ctx.Value(correlationIDKey).(string)
	return v
}

// TenantFromContext 返回已注入上下文的租户 ID，缺失时返回空字符串。
func TenantFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	v, _ := ctx.Value(tenantIDKey).(string)
	return v
}

// WithTenant 将租户 ID 写入上下文，供 Authz 中间件向下游处理链传播。
func WithTenant(ctx context.Context, tenantID string) context.Context {
	return context.WithValue(ctx, tenantIDKey, tenantID)
}

// newCorrelationID 生成一个 128-bit 的十六进制关联 ID（32 个字符）。
func newCorrelationID() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// fallbackCorrelationID 在 crypto/rand 不可用时退化为基于纳秒时间戳的 ID，
// 保证关联能力不因熵源异常而中断（概率极低，但不应阻断请求）。
func fallbackCorrelationID() string {
	return fmt.Sprintf("ts-%s", strconv.FormatInt(time.Now().UnixNano(), 16))
}

// Correlation 返回注入关联 ID 的中间件。
//
// 行为：
//   - 若入站请求缺失关联 ID，则以 crypto/rand 生成一个并回填请求头。
//   - 将关联 ID 注入 context（供日志/链路追踪读取）与响应头。
//   - 若入站已携带关联 ID，则原样透传，保证跨服务链路连续。
func Correlation(header string) Middleware {
	if header == "" {
		header = DefaultCorrelationHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			cid := r.Header.Get(header)
			if cid == "" {
				id, err := newCorrelationID()
				if err != nil {
					id = fallbackCorrelationID()
				}
				cid = id
			}

			// 回填请求头（下游反代/日志可见）与响应头（客户端可见）。
			r.Header.Set(header, cid)
			w.Header().Set(header, cid)

			ctx := context.WithValue(r.Context(), correlationIDKey, cid)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}
