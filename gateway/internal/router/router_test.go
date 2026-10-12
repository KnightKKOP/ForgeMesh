package router

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
)

// newTestRouter 构造 dev 认证模式的测试用网关处理器；upstreamURL 指向被测的模拟上游。
func newTestRouter(t *testing.T, upstreamURL string) *Router {
	t.Helper()
	cfg := config.Config{
		ListenAddr:        ":0",
		UpstreamURL:       upstreamURL,
		RateLimitRPS:      1000,
		RateLimitBurst:    1000,
		TenantHeader:      "X-Tenant-ID",
		CorrelationHeader: "X-Correlation-ID",
		AuthMode:          config.AuthModeDev,
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

// TestInferenceChat_ResolvesEndpoint 端到端验证推理路由：
// 控制平面返回 Ready + 实例 endpoint，SSE 请求应被转发到该实例并透传流式响应。
func TestInferenceChat_ResolvesEndpoint(t *testing.T) {
	sseData := "data: token-1\n\ndata: token-2\n\n"
	var instanceGotPath, instanceGotTenant string
	instance := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		instanceGotPath = req.URL.Path
		instanceGotTenant = req.Header.Get("X-Tenant-ID")
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(sseData))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer instance.Close()

	// 模拟控制平面：GET /api/v1/inference-services/{svc} 返回实例地址。
	controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/api/v1/inference-services/svc-a" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, fmt.Sprintf(
			`{"apiVersion":"forgemesh/v1","kind":"InferenceService","metadata":{"name":"svc-a"},`+
				`"spec":{},"status":{"phase":"Ready","endpoints":[%q]}}`, instance.URL))
	}))
	defer controlPlane.Close()

	r := newTestRouter(t, controlPlane.URL)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("推理请求应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	if got := rec.Body.String(); got != sseData {
		t.Fatalf("SSE 数据应原样透传，\nwant %q\ngot  %q", sseData, got)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type 应为 text/event-stream，实际 %q", ct)
	}
	if instanceGotPath != "/api/v1/inference/svc-a/chat" {
		t.Fatalf("实例收到路径不符: %q", instanceGotPath)
	}
	if instanceGotTenant != "tenant-a" {
		t.Fatalf("租户头应透传到实例，实际 %q", instanceGotTenant)
	}
}

// TestInferenceChat_ServiceNotFound 验证服务不存在时返回 503 与 NO_HEALTHY_UPSTREAM 信封。
func TestInferenceChat_ServiceNotFound(t *testing.T) {
	controlPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"detail":"not found"}`)
	}))
	defer controlPlane.Close()

	r := newTestRouter(t, controlPlane.URL)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/nope/chat", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("服务不存在应 503，实际 %d", rec.Code)
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"code":"NO_HEALTHY_UPSTREAM"`) {
		t.Fatalf("错误体应含 NO_HEALTHY_UPSTREAM，实际: %s", body)
	}
}
