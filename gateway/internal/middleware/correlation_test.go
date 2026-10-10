package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// TestCorrelation_GeneratesWhenMissing 验证缺失关联 ID 时生成并注入 context 与响应头。
func TestCorrelation_GeneratesWhenMissing(t *testing.T) {
	const header = "X-Correlation-ID"

	var ctxID, reqHeader string
	h := Correlation(header)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctxID = CorrelationIDFromContext(r.Context())
		reqHeader = r.Header.Get(header)
		w.WriteHeader(http.StatusOK)
	}))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	h.ServeHTTP(rec, req)

	if ctxID == "" {
		t.Fatal("context 中未注入关联 ID")
	}
	if reqHeader != ctxID {
		t.Fatalf("请求头关联 ID(%q) 与 context(%q) 不一致", reqHeader, ctxID)
	}
	if got := rec.Header().Get(header); got != ctxID {
		t.Fatalf("响应头关联 ID(%q) 与 context(%q) 不一致", got, ctxID)
	}
	// 128-bit hex → 32 字符。
	if len(ctxID) != 32 {
		t.Fatalf("关联 ID 长度应为 32，实际 %d", len(ctxID))
	}
}

// TestCorrelation_PreservesInbound 验证入站已带关联 ID 时原样透传（跨服务链路连续）。
func TestCorrelation_PreservesInbound(t *testing.T) {
	const (
		header  = "X-Trace-ID"
		inbound = "upstream-corr-123"
	)

	var ctxID string
	h := Correlation(header)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctxID = CorrelationIDFromContext(r.Context())
	}))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	req.Header.Set(header, inbound)
	h.ServeHTTP(rec, req)

	if ctxID != inbound {
		t.Fatalf("应透传入站关联 ID %q，实际 %q", inbound, ctxID)
	}
	if got := rec.Header().Get(header); got != inbound {
		t.Fatalf("响应头应透传 %q，实际 %q", inbound, got)
	}
}

// TestCorrelation_DefaultHeader 验证空 header 参数时回退到默认头名。
func TestCorrelation_DefaultHeader(t *testing.T) {
	var ctxID string
	h := Correlation("")(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctxID = CorrelationIDFromContext(r.Context())
	}))
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	h.ServeHTTP(rec, req)
	if ctxID == "" {
		t.Fatal("默认头名下未注入关联 ID")
	}
	if rec.Header().Get(DefaultCorrelationHeader) == "" {
		t.Fatal("响应缺少默认关联头")
	}
}
