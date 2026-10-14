package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// doTenantRequest 以指定的已认证租户发送一次请求（租户为空表示未认证），返回响应记录器。
func doTenantRequest(h http.Handler, tenant string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	if tenant != "" {
		req = req.WithContext(WithPrincipal(req.Context(), Principal{TenantID: tenant}))
	}
	h.ServeHTTP(rec, req)
	return rec
}

func newLimitedHandler(rps float64, burst int) http.Handler {
	return RateLimit(NewRateLimiter(rps, burst))(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
}

// TestRateLimit_RejectsOverBurst 验证超出桶容量后返回 429 且带 Retry-After。
func TestRateLimit_RejectsOverBurst(t *testing.T) {
	// 极低补充速率，保证测试窗口内令牌不会自然回填。
	h := newLimitedHandler(0.0001, 2)

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
	h := newLimitedHandler(0.0001, 1)

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

// TestRateLimit_AnonymousFallsBackToIP 验证无已认证租户时退化为按 IP 限流。
func TestRateLimit_AnonymousFallsBackToIP(t *testing.T) {
	h := newLimitedHandler(0.0001, 1)

	if rec := doTenantRequest(h, ""); rec.Code != http.StatusOK {
		t.Fatalf("匿名首次应放行，实际 %d", rec.Code)
	}
	if rec := doTenantRequest(h, ""); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("匿名二次应 429，实际 %d", rec.Code)
	}
}

// TestRateLimit_IgnoresClientControlledHeaders 验证轮换租户头或 X-Forwarded-For
// 无法获得新的令牌桶：key 只取已认证的租户与 TCP 对端地址。
func TestRateLimit_IgnoresClientControlledHeaders(t *testing.T) {
	cases := map[string]struct {
		tenant string
		header string
	}{
		"已认证请求轮换租户头": {tenant: "tenant-a", header: DefaultTenantHeader},
		"匿名请求轮换租户头":  {header: DefaultTenantHeader},
		"匿名请求轮换 XFF": {header: "X-Forwarded-For"},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			h := newLimitedHandler(0.0001, 1)
			for i, value := range []string{"10.0.0.1", "10.0.0.2"} {
				rec := httptest.NewRecorder()
				req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
				req.Header.Set(tc.header, value)
				if tc.tenant != "" {
					req = req.WithContext(WithPrincipal(req.Context(), Principal{TenantID: tc.tenant}))
				}
				h.ServeHTTP(rec, req)

				want := http.StatusOK
				if i == 1 {
					want = http.StatusTooManyRequests
				}
				if rec.Code != want {
					t.Fatalf("第 %d 次请求（%s=%s）应 %d，实际 %d", i+1, tc.header, value, want, rec.Code)
				}
			}
		})
	}
}

// TestLimiter_CountsRejections 验证 Rejected 累计各个 key 被拒绝的次数，放行的请求不计。
func TestLimiter_CountsRejections(t *testing.T) {
	l := NewRateLimiter(0.0001, 2)
	h := RateLimit(l)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))

	for i := 0; i < 5; i++ {
		rec := doTenantRequest(h, "tenant-a")
		if i >= 2 && rec.Code != http.StatusTooManyRequests {
			t.Fatalf("第 %d 次请求应 429，实际 %d", i+1, rec.Code)
		}
	}
	doTenantRequest(h, "tenant-b")
	if got := l.Rejected(); got != 3 {
		t.Fatalf("应累计拒绝 3 次，实际 %d", got)
	}
}
