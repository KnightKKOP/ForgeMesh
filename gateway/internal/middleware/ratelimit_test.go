package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// doTenantRequest 以指定租户发送一次请求，返回响应记录器。
func doTenantRequest(h http.Handler, tenant string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	if tenant != "" {
		req.Header.Set(DefaultTenantHeader, tenant)
	}
	h.ServeHTTP(rec, req)
	return rec
}

// TestRateLimit_RejectsOverBurst 验证超出桶容量后返回 429 且带 Retry-After。
func TestRateLimit_RejectsOverBurst(t *testing.T) {
	// 极低补充速率，保证测试窗口内令牌不会自然回填。
	limiter := NewRateLimiter(0.0001, 2)
	h := RateLimit(limiter, DefaultTenantHeader)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	for i := 1; i <= 2; i++ {
		if rec := doTenantRequest(h, "tenant-a"); rec.Code != http.StatusOK {
			t.Fatalf("第 %d 次请求应放行(200)，实际 %d", i, rec.Code)
		}
	}

	rec := doTenantRequest(h, "tenant-a")
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("超限请求应返回 429，实际 %d", rec.Code)
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("429 响应缺少 Retry-After 头")
	}
}

// TestRateLimit_TenantIsolation 验证限流按租户隔离：一个租户耗尽不影响其他租户。
func TestRateLimit_TenantIsolation(t *testing.T) {
	limiter := NewRateLimiter(0.0001, 1)
	h := RateLimit(limiter, DefaultTenantHeader)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	if rec := doTenantRequest(h, "tenant-a"); rec.Code != http.StatusOK {
		t.Fatalf("tenant-a 首次应放行，实际 %d", rec.Code)
	}
	if rec := doTenantRequest(h, "tenant-a"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("tenant-a 二次应 429，实际 %d", rec.Code)
	}
	// 另一租户拥有独立桶，应仍可放行。
	if rec := doTenantRequest(h, "tenant-b"); rec.Code != http.StatusOK {
		t.Fatalf("tenant-b 应独立放行，实际 %d", rec.Code)
	}
}

// TestRateLimit_AnonymousFallsBackToIP 验证无租户头时退化为按 IP 限流。
func TestRateLimit_AnonymousFallsBackToIP(t *testing.T) {
	limiter := NewRateLimiter(0.0001, 1)
	h := RateLimit(limiter, DefaultTenantHeader)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	if rec := doTenantRequest(h, ""); rec.Code != http.StatusOK {
		t.Fatalf("匿名首次应放行，实际 %d", rec.Code)
	}
	if rec := doTenantRequest(h, ""); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("匿名二次应 429，实际 %d", rec.Code)
	}
}
