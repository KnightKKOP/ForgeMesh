// chain_test.go 验证中间件链的组合语义（WP-D1）：
//
// 链序（与 router.go 实际组装一致，外层 → 内层；更外层的 Observe 见 observe_test.go）：
//
//		Recovery → Correlation → TraceContext → AccessLog → Authz → RateLimit → LimitRequestBody → Route
//
//	  - Correlation 先于 Authz：认证失败的响应也携带关联 ID；
//	  - Authz 先于 RateLimit：被拒请求不产生限流副作用（不耗令牌、不误伤）；
//	  - Recovery 最外层：下游 panic 兜底为 500，不泄露堆栈；
//	  - 探针式直连路径（模仿 router.go 顶层 /healthz）不经过链、不泄漏受信头；
//	  - 各类拒绝响应为固定文案，不含内部细节。
//
// 链中的 Authz 是 dev 模式的 Authenticate（信任租户头），链序语义与 apikey 模式相同。
package middleware

import (
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// newAPIChain 按 router.go 的实际组装构造 API 链（外层再包 Recovery）。
// limiter 由调用方注入以便观测副作用。
func newAPIChain(l *Limiter, route http.Handler) http.Handler {
	if route == nil {
		route = okHandler
	}
	api := Chain(route,
		Correlation(DefaultCorrelationHeader),
		TraceContext(),
		AccessLog(log.New(io.Discard, "", 0)),
		Authz(DefaultTenantHeader),
		RateLimit(l),
		LimitRequestBody(1<<20),
	)
	return Recovery(log.New(io.Discard, "", 0))(api)
}

// TestChain_CorrelationBeforeAuthz 验证 Correlation 先于 Authz 执行：
// 认证失败（401）的响应仍携带关联 ID，便于客户端报障与日志关联。
func TestChain_CorrelationBeforeAuthz(t *testing.T) {
	h := newAPIChain(NewRateLimiter(1000, 1000), nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺租户头应 401，实际 %d", rec.Code)
	}
	if rec.Header().Get(DefaultCorrelationHeader) == "" {
		t.Fatal("401 响应应携带关联 ID（Correlation 先于 Authz 执行）")
	}
}

// TestChain_AuthzBeforeRateLimitNoSideEffect 验证 Authz 先于 RateLimit 且
// 拒绝路径无副作用：burst=1、rps≈0 的桶若被匿名请求（ip key）消耗，
// 第二个匿名请求将变为 429；实际连续 3 个匿名请求均应稳定 401。
func TestChain_AuthzBeforeRateLimitNoSideEffect(t *testing.T) {
	h := newAPIChain(NewRateLimiter(0.001, 1), nil) // 极慢补充 + 单令牌

	for i := 0; i < 3; i++ {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("第 %d 个匿名请求应 401（若 RateLimit 先执行则会 429），实际 %d", i+1, rec.Code)
		}
	}
}

// TestChain_RateLimitKeysTenantAfterAuthz 验证 RateLimit 位于 Authz 之后且
// 按租户 key 限流：同租户第二个请求 429（附 Retry-After），异租户不受牵连。
func TestChain_RateLimitKeysTenantAfterAuthz(t *testing.T) {
	h := newAPIChain(NewRateLimiter(0.001, 1), nil) // 极慢补充 + 单令牌

	do := func(tenant string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
		if tenant != "" {
			req.Header.Set(DefaultTenantHeader, tenant)
		}
		h.ServeHTTP(rec, req)
		return rec
	}

	if rec := do("tenant-a"); rec.Code != http.StatusOK {
		t.Fatalf("tenant-a 首个请求应 200，实际 %d", rec.Code)
	}
	rec := do("tenant-a")
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("tenant-a 第二个请求应 429，实际 %d", rec.Code)
	}
	if rec.Header().Get("Retry-After") == "" {
		t.Fatal("429 响应应携带 Retry-After")
	}
	if rec2 := do("tenant-b"); rec2.Code != http.StatusOK {
		t.Fatalf("tenant-b 应有独立令牌桶（200），实际 %d", rec2.Code)
	}
}

// TestChain_RecoveryOutermostOpaque500 验证 Recovery 位于最外层：
// 下游 handler panic 被兜底为 500 INTERNAL_ERROR 错误信封（固定文案，无堆栈/源码路径），
// 响应头与信封都带关联 ID（Correlation 位于其内侧，已先行写入响应头并记录给 Recovery）。
func TestChain_RecoveryOutermostOpaque500(t *testing.T) {
	panicRoute := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic("boom: 内部敏感细节")
	})
	h := newAPIChain(NewRateLimiter(1000, 1000), panicRoute)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("panic 应兜底 500，实际 %d", rec.Code)
	}
	env := readEnvelope(t, rec)
	if env.Code != "INTERNAL_ERROR" || env.Message != "internal server error" {
		t.Fatalf("500 应为固定文案的 INTERNAL_ERROR 错误信封，实际 %+v", env)
	}
	corr := rec.Header().Get(DefaultCorrelationHeader)
	if corr == "" || env.CorrelationID != corr || len(env.TraceID) != 32 {
		t.Fatalf("响应头与信封应带同一个关联 ID（%q）及 trace-id，实际 %+v", corr, env)
	}
	if strings.Contains(rec.Body.String(), "boom") {
		t.Fatalf("500 响应体不应包含 panic 的内容: %s", rec.Body.String())
	}
}

// TestChain_ProbeBypassesChainNoLeak 验证探针式直连路径不泄漏链路头：
// 模仿 router.go 顶层组装（/healthz 直连探针、/api/v1/ 前缀走链），
// 探针响应 200 且不携带 X-Correlation-ID / X-Tenant-ID 等受信头。
func TestChain_ProbeBypassesChainNoLeak(t *testing.T) {
	probe := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	root := http.NewServeMux()
	root.Handle("/healthz", probe)
	root.Handle("/api/v1/", newAPIChain(NewRateLimiter(1000, 1000), nil))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	// 刻意携带受信头，验证探针路径不会回显/泄漏任何链路注入头。
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	req.Header.Set(DefaultCorrelationHeader, "corr-should-not-echo")
	root.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("探针应 200，实际 %d", rec.Code)
	}
	for _, h := range []string{DefaultCorrelationHeader, DefaultTenantHeader, "Retry-After"} {
		if v := rec.Header().Get(h); v != "" {
			t.Fatalf("探针响应不应携带链路头 %s，实际 %q", h, v)
		}
	}
	if body := rec.Body.String(); body != "ok" {
		t.Fatalf("探针响应体应为 ok，实际 %q", body)
	}
}

// TestChain_RejectResponsesOpaque 验证认证/限流失败响应不含内部细节：
// 均为固定文案的错误信封，无堆栈、源码路径或实现细节。
func TestChain_RejectResponsesOpaque(t *testing.T) {
	h := newAPIChain(NewRateLimiter(0.001, 1), nil)

	// 401：缺失租户身份，返回固定文案的错误信封。
	rec401 := httptest.NewRecorder()
	h.ServeHTTP(rec401, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))
	if code := decodeError(t, rec401); code != "UNAUTHENTICATED" ||
		!strings.Contains(rec401.Body.String(), `"message":"missing tenant identity"`) {
		t.Fatalf("401 响应体应为 UNAUTHENTICATED 错误信封，实际 %q", rec401.Body.String())
	}

	// 429：同租户超限（首请求已消耗 burst=1 的唯一令牌）。
	req1 := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req1.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(httptest.NewRecorder(), req1)
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req2.Header.Set(DefaultTenantHeader, "tenant-a")
	rec429 := httptest.NewRecorder()
	h.ServeHTTP(rec429, req2)
	if env := readEnvelope(t, rec429); env.Code != "RATE_LIMITED" || env.Message != "rate limit exceeded" ||
		env.CorrelationID != rec429.Header().Get(DefaultCorrelationHeader) {
		t.Fatalf("429 响应体应为带关联 ID 的 RATE_LIMITED 错误信封，实际 %q", rec429.Body.String())
	}

	// 内部细节泄漏检查（401/429 两类错误信封）。
	for name, body := range map[string]string{"401": rec401.Body.String(), "429": rec429.Body.String()} {
		for _, leak := range []string{"goroutine", "panic", ".go:", "runtime", "token", "bucket"} {
			if strings.Contains(strings.ToLower(body), leak) {
				t.Fatalf("%s 响应体泄漏内部细节 %q: %s", name, leak, body)
			}
		}
	}
}

// TestChain_CorrelationGeneratePropagateUnique 验证关联 ID 语义：
// 缺失时生成（32 位 hex）、注入下游请求头与 context 并回写响应头；
// 入站已携带时原样透传；不同请求生成的 ID 互不相同。
func TestChain_CorrelationGeneratePropagateUnique(t *testing.T) {
	var gotHeader, gotCtx string
	route := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotHeader = r.Header.Get(DefaultCorrelationHeader)
		gotCtx = CorrelationIDFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	})
	h := Chain(route, Correlation(DefaultCorrelationHeader))

	// 入站携带 → 原样透传且贯穿 context/请求头/响应头。
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultCorrelationHeader, "corr-inbound-42")
	h.ServeHTTP(rec, req)

	if gotHeader != "corr-inbound-42" || gotCtx != "corr-inbound-42" {
		t.Fatalf("入站关联 ID 应透传（头=%q ctx=%q）", gotHeader, gotCtx)
	}
	if v := rec.Header().Get(DefaultCorrelationHeader); v != "corr-inbound-42" {
		t.Fatalf("响应头关联 ID 应为 corr-inbound-42，实际 %q", v)
	}

	// 缺失 → 生成 32 位 hex 并回填；两次请求生成的 ID 不同。
	gen1 := serveForCID(h)
	gen2 := serveForCID(h)
	for name, cid := range map[string]string{"第一次": gen1, "第二次": gen2} {
		if len(cid) != 32 {
			t.Fatalf("%s生成的关联 ID 应为 32 位 hex，实际 %q（长 %d）", name, cid, len(cid))
		}
	}
	if gen1 == gen2 {
		t.Fatalf("两次请求不应生成相同关联 ID: %q", gen1)
	}
}

// serveForCID 经链发起无关联 ID 请求，返回生成的关联 ID（响应头）。
func serveForCID(h http.Handler) string {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))
	return rec.Header().Get(DefaultCorrelationHeader)
}
