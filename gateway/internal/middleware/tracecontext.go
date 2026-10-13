package middleware

import (
	"context"
	"fmt"
	"math/rand/v2"
	"net/http"
	"regexp"
)

// W3C Trace Context（https://www.w3.org/TR/trace-context/）的请求头（HTTP 头大小写不敏感）。
const (
	TraceparentHeader = "traceparent"
	TracestateHeader  = "tracestate"
)

var traceparentPattern = regexp.MustCompile(`^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}`)

const (
	traceparentV00Length = 55
	invalidTraceID       = "00000000000000000000000000000000"
	invalidParentID      = "0000000000000000"
)

// randUint64 生成 trace-id 与 parent-id。math/rand/v2 的全局生成器由运行时的 ChaCha8 提供、
// 按进程随机播种：取值不可预测，且没有 crypto/rand 那样需要处理的出错路径。
var randUint64 = rand.Uint64

// ParseTraceparent 按 W3C Trace Context 解析 traceparent，返回其中的 trace-id；不合法时返回
// false。只接受小写十六进制；trace-id / parent-id 全零、版本 ff 都不合法；版本 00 必须恰好
// 55 个字符，更高的版本按前 55 个字符解析，之后若还有内容须以 "-" 分隔。
func ParseTraceparent(value string) (traceID string, ok bool) {
	m := traceparentPattern.FindStringSubmatch(value)
	if m == nil {
		return "", false
	}
	version, traceID, parentID := m[1], m[2], m[3]
	wellFormed := len(value) == traceparentV00Length
	if version != "00" {
		wellFormed = version != "ff" && (wellFormed || value[traceparentV00Length] == '-')
	}
	if !wellFormed || traceID == invalidTraceID || parentID == invalidParentID {
		return "", false
	}
	return traceID, true
}

// newTraceparent 生成新追踪的 traceparent（版本 00；flags 00 即未采样：网关不记录 span），
// 返回它与其中的 trace-id。
func newTraceparent() (traceparent, traceID string) {
	var high, low, parent uint64
	for high == 0 && low == 0 {
		high, low = randUint64(), randUint64()
	}
	for parent == 0 {
		parent = randUint64()
	}
	traceID = fmt.Sprintf("%016x%016x", high, low)
	return fmt.Sprintf("00-%s-%016x-00", traceID, parent), traceID
}

// traceInfo 是请求的追踪上下文：转发的 traceparent 与其中的 trace-id。
type traceInfo struct {
	traceparent string
	traceID     string
}

type traceInfoKey struct{}

// TraceContext 返回确定每个请求追踪上下文的中间件（网关不记录 span，也不导出追踪）：
//
//   - 请求恰好带一个合法的 traceparent 时原样沿用，tracestate 一并保留；
//   - 否则（缺失、不合法或多个）开始新的追踪：生成新的 traceparent，丢弃 tracestate；
//   - 确定的 traceparent 写回请求头，随请求转发给控制平面与推理实例（Resolver 的查询经
//     TraceparentFromContext 附带），trace-id 注入 context 供日志与错误信封读取，并记录给
//     外层的 Recovery。
//
// 响应不回写 traceparent：客户端凭响应头中的关联 ID 或错误信封中的 traceId 报障。
func TraceContext() Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var info traceInfo
			if values := r.Header.Values(TraceparentHeader); len(values) == 1 {
				if traceID, ok := ParseTraceparent(values[0]); ok {
					info = traceInfo{traceparent: values[0], traceID: traceID}
				}
			}
			if info.traceID == "" {
				info.traceparent, info.traceID = newTraceparent()
				r.Header.Set(TraceparentHeader, info.traceparent)
				r.Header.Del(TracestateHeader)
			}
			if ids := requestIDsFrom(r.Context()); ids != nil {
				ids.traceID = info.traceID
			}
			next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), traceInfoKey{}, info)))
		})
	}
}

// TraceIDFromContext 返回请求的 trace-id（32 个小写十六进制字符），缺失时返回空字符串。
func TraceIDFromContext(ctx context.Context) string {
	return traceInfoFrom(ctx).traceID
}

// TraceparentFromContext 返回请求转发的 traceparent，缺失时返回空字符串。
func TraceparentFromContext(ctx context.Context) string {
	return traceInfoFrom(ctx).traceparent
}

func traceInfoFrom(ctx context.Context) traceInfo {
	if ctx == nil {
		return traceInfo{}
	}
	info, _ := ctx.Value(traceInfoKey{}).(traceInfo)
	return info
}
