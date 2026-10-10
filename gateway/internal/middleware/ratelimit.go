package middleware

import (
	"math"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
)

// DefaultTenantHeader 是默认的租户身份请求头名称。
const DefaultTenantHeader = "X-Tenant-ID"

// idleTTL 是空闲令牌桶的保留时长；超过该时长且未被访问的桶会在惰性 GC 中被回收，
// 避免恶意租户通过海量伪造 key 造成内存无限增长。
const idleTTL = 10 * time.Minute

// gcInterval 是惰性 GC 的最小触发间隔。
const gcInterval = time.Minute

// Limiter 是自实现的按 key 令牌桶限流器，仅依赖标准库。
//
// 算法：
//   - 每个 key（租户）维护一个令牌桶，容量为 burst，补充速率为 rps。
//   - 每次请求消耗 1 个令牌；不足则拒绝，并给出建议重试时长。
//   - 令牌补充采用惰性计算：仅在请求到达时按流逝时间补足，无需后台定时器。
//
// 并发安全：所有桶操作由互斥锁保护。
type Limiter struct {
	rps   float64
	burst float64

	mu      sync.Mutex
	buckets map[string]*bucket
	now     func() time.Time // 注入式时钟，便于单元测试与时间推进
	lastGC  time.Time
}

// bucket 表示单个 key 的令牌桶状态。
type bucket struct {
	tokens float64   // 当前剩余令牌数
	last   time.Time // 上次补充令牌的时间
	seen   time.Time // 上次访问时间，用于空闲回收
}

// NewRateLimiter 构造令牌桶限流器。
// rps 为每秒补充的令牌数；burst 为桶容量。非法入参会被夹取到最小值 1。
func NewRateLimiter(rps float64, burst int) *Limiter {
	if rps <= 0 {
		rps = 1
	}
	if burst <= 0 {
		burst = 1
	}
	return &Limiter{
		rps:     rps,
		burst:   float64(burst),
		buckets: make(map[string]*bucket),
		now:     time.Now,
		lastGC:  time.Now(),
	}
}

// Allow 判断 key 对应的请求是否放行。
//
// 返回：
//   - allowed=true：放行，retryAfter 为 0。
//   - allowed=false：超限，retryAfter 为建议的等待时长（供 Retry-After 头使用）。
func (l *Limiter) Allow(key string) (allowed bool, retryAfter time.Duration) {
	l.mu.Lock()
	defer l.mu.Unlock()

	now := l.now()
	l.maybeGC(now)

	b, ok := l.buckets[key]
	if !ok {
		b = &bucket{tokens: l.burst, last: now}
		l.buckets[key] = b
	}

	// 按流逝时间补充令牌，并以桶容量封顶。
	if elapsed := now.Sub(b.last).Seconds(); elapsed > 0 {
		b.tokens = math.Min(l.burst, b.tokens+elapsed*l.rps)
		b.last = now
	}
	b.seen = now

	if b.tokens >= 1 {
		b.tokens--
		return true, 0
	}

	// 令牌不足：计算补足到 1 个令牌所需的等待时长。
	deficit := 1 - b.tokens
	retry := time.Duration(deficit / l.rps * float64(time.Second))
	if retry <= 0 {
		// 至少给 1ms，避免零值导致 Retry-After 缺失。
		retry = time.Millisecond
	}
	return false, retry
}

// maybeGC 惰性回收长时间空闲的桶。调用方必须持有锁。
func (l *Limiter) maybeGC(now time.Time) {
	if now.Sub(l.lastGC) < gcInterval {
		return
	}
	l.lastGC = now
	for k, b := range l.buckets {
		if now.Sub(b.seen) > idleTTL {
			delete(l.buckets, k)
		}
	}
}

// RateLimit 返回按租户限流的中间件。
//
// key 选取优先使用请求头中的租户身份；若缺失（尚未经过鉴权或匿名流量），
// 退化为按客户端 IP 限流，保证入口不被单点打爆。
func RateLimit(l *Limiter, tenantHeader string) Middleware {
	if l == nil {
		panic("middleware: RateLimit 需要非空 Limiter")
	}
	if tenantHeader == "" {
		tenantHeader = DefaultTenantHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			key := rateLimitKey(r, tenantHeader)
			allowed, retryAfter := l.Allow(key)
			if !allowed {
				// Retry-After 采用秒级向上取整，最小为 1。
				secs := int(math.Ceil(retryAfter.Seconds()))
				if secs < 1 {
					secs = 1
				}
				w.Header().Set("Retry-After", strconv.Itoa(secs))
				http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// rateLimitKey 计算限流 key：租户头 → 上下文租户 → 客户端 IP。
func rateLimitKey(r *http.Request, tenantHeader string) string {
	if tenant := strings.TrimSpace(r.Header.Get(tenantHeader)); tenant != "" {
		return "tenant:" + tenant
	}
	if tenant := TenantFromContext(r.Context()); tenant != "" {
		return "tenant:" + tenant
	}
	return "ip:" + clientIP(r)
}

// clientIP 尽力提取客户端 IP；优先 X-Forwarded-For 首段，否则使用 RemoteAddr。
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if idx := strings.IndexByte(xff, ','); idx >= 0 {
			xff = xff[:idx]
		}
		if ip := strings.TrimSpace(xff); ip != "" {
			return ip
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}
