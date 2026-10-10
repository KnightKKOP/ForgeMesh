package router

import (
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
)

// newTestRouter 构造测试用网关处理器；upstreamURL 指向被测的模拟上游。
func newTestRouter(t *testing.T, upstreamURL string) *Router {
	t.Helper()
	cfg := config.Config{
		ListenAddr:        ":0",
		UpstreamURL:       upstreamURL,
		RateLimitRPS:      1000,
		RateLimitBurst:    1000,
		TenantHeader:      "X-Tenant-ID",
		CorrelationHeader: "X-Correlation-ID",
	}
	r, err := New(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	return r
}

// TestHealthz_Returns200 验证存活探针可用。
func TestHealthz_Returns200(t *testing.T) {
	r := newTestRouter(t, "http://127.0.0.1:1")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("healthz 应 200，实际 %d", rec.Code)
	}
}

// TestApi_MissingTenant_Returns401 验证缺失租户身份时鉴权中间件拦截。
func TestApi_MissingTenant_Returns401(t *testing.T) {
	r := newTestRouter(t, "http://127.0.0.1:1")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺租户应 401，实际 %d", rec.Code)
	}
}

// TestApi_ProxiesToUpstream 验证带租户身份的资源请求被反代到上游，
// 且关联 ID 与租户头均透传。
func TestApi_ProxiesToUpstream(t *testing.T) {
	var gotCorr, gotTenant, gotPath string
	upstreamSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCorr = r.Header.Get("X-Correlation-ID")
		gotTenant = r.Header.Get("X-Tenant-ID")
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "ok")
	}))
	defer upstreamSrv.Close()

	r := newTestRouter(t, upstreamSrv.URL)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("反代应 200，实际 %d", rec.Code)
	}
	if rec.Body.String() != "ok" {
		t.Fatalf("响应体应为 ok，实际 %q", rec.Body.String())
	}
	if gotPath != "/api/v1/resources" {
		t.Fatalf("上游路径应保留 /api/v1/resources，实际 %q", gotPath)
	}
	if gotTenant != "tenant-a" {
		t.Fatalf("租户头应透传 tenant-a，实际 %q", gotTenant)
	}
	if gotCorr == "" {
		t.Fatal("关联 ID 应被生成并透传到上游")
	}
}

// TestReadyz_503WhenUpstreamUnreachable 验证上游不可达时就绪探针返回 503。
func TestReadyz_503WhenUpstreamUnreachable(t *testing.T) {
	r := newTestRouter(t, "http://127.0.0.1:1")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("上游不可达应 503，实际 %d", rec.Code)
	}
}

// TestApi_UnknownRouteAllowedThroughChain 验证未知 API 子路径经过中间件链后返回 404。
func TestApi_UnknownRouteAllowedThroughChain(t *testing.T) {
	r := newTestRouter(t, "http://127.0.0.1:1")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/unknown-route", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("未知路由应 404，实际 %d", rec.Code)
	}
	// 关联 ID 中间件应已生效。
	if rec.Header().Get("X-Correlation-ID") == "" {
		t.Fatal("未知路由也应携带关联 ID 响应头")
	}
}
