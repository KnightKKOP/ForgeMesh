// metrics_test.go 从网关入口验证 /metrics：
//
//   - 与探针一样不需要凭证、不经过限流，只响应 GET / HEAD；
//   - 请求按路由类别与状态码计数（被认证拒绝的请求按目标路由计）；
//   - 各组件的指标都已接入：限流拒绝、Resolver 查询、首字节时间、实例状态、上游错误；
//   - 输出不含租户、服务名、凭证主体、路径与实例地址。
package router

import (
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics/metricstest"
)

// scrapeText 不带凭证抓取 /metrics，返回响应体。
func scrapeText(t *testing.T, r *Router) string {
	t.Helper()
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("/metrics 应 200，实际 %d", rec.Code)
	}
	if got := rec.Header().Get("Content-Type"); got != metrics.ContentType {
		t.Fatalf("Content-Type 应为 %q，实际 %q", metrics.ContentType, got)
	}
	return rec.Body.String()
}

func requestsKey(route metrics.Route, status string) string {
	return `forgemesh_gateway_http_requests_total{route="` + string(route) + `",status="` + status + `"}`
}

func assertSamples(t *testing.T, samples, want map[string]float64) {
	t.Helper()
	for key, value := range want {
		if got, ok := samples[key]; !ok || got != value {
			t.Errorf("%s = %v（存在 %v），want %v", key, got, ok, value)
		}
	}
}

// TestMetrics_ServedWithoutCredentials 验证 apikey 模式下 /metrics 不需要凭证，输出合法的
// 文本格式；HEAD 不带响应体，其他方法 405。
func TestMetrics_ServedWithoutCredentials(t *testing.T) {
	r := newAPIKeyRouter(t, "http://127.0.0.1:1")

	samples := metricstest.MustParse(t, scrapeText(t, r))
	assertSamples(t, samples, map[string]float64{
		"forgemesh_gateway_rate_limited_total":                    0,
		`forgemesh_gateway_resolver_lookups_total{result="miss"}`: 0,
		"forgemesh_gateway_stream_first_byte_seconds_count":       0,
		`forgemesh_gateway_instances{state="healthy"}`:            0,
		`forgemesh_gateway_instances{state="unhealthy"}`:          0,
	})

	head := httptest.NewRecorder()
	r.ServeHTTP(head, httptest.NewRequest(http.MethodHead, "/metrics", nil))
	if head.Code != http.StatusOK || head.Body.Len() != 0 {
		t.Fatalf("HEAD 应 200 且没有响应体，实际 %d（%d 字节）", head.Code, head.Body.Len())
	}
	post := httptest.NewRecorder()
	r.ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/metrics", nil))
	if post.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST 应 405，实际 %d", post.Code)
	}
}

// TestMetrics_CountsRequestsByRouteClass 验证请求按路由类别与状态码计数，推理请求的 Resolver
// 查询、首字节时间与实例状态可见，且输出不含租户、服务名、凭证主体、路径与实例地址。
func TestMetrics_CountsRequestsByRouteClass(t *testing.T) {
	env := newInferenceEnv(t, http.StatusOK)
	if rec := env.chat(routerKeyTenantA); rec.Code != http.StatusOK {
		t.Fatalf("推理请求应 200，实际 %d", rec.Code)
	}
	for _, c := range []struct {
		path, key string
		want      int
	}{
		{"/api/v1/inference-services", routerKeyTenantA, http.StatusOK},
		{"/api/v1/audit-events", routerKeyTenantA, http.StatusOK},
		{"/api/v1/resources", "", http.StatusUnauthorized},
		{"/api/v1/unknown", routerKeyTenantA, http.StatusNotFound},
		{"/unknown", "", http.StatusNotFound},
		{"/healthz", "", http.StatusOK},
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, c.path, nil)
		if c.key != "" {
			req.Header.Set("Authorization", "Bearer "+c.key)
		}
		env.router.ServeHTTP(rec, req)
		if rec.Code != c.want {
			t.Fatalf("%s 应 %d，实际 %d", c.path, c.want, rec.Code)
		}
	}
	scrapeText(t, env.router)

	text := scrapeText(t, env.router)
	assertSamples(t, metricstest.MustParse(t, text), map[string]float64{
		requestsKey(metrics.RouteInference, "200"):                               1,
		requestsKey(metrics.RouteControlPlane, "200"):                            2,
		requestsKey(metrics.RouteControlPlane, "401"):                            1,
		requestsKey(metrics.RouteUnmatched, "404"):                               2,
		requestsKey(metrics.RouteProbe, "200"):                                   1,
		requestsKey(metrics.RouteMetrics, "200"):                                 1,
		`forgemesh_gateway_http_request_duration_seconds_count{route="probe"}`:   1,
		`forgemesh_gateway_resolver_lookups_total{result="miss"}`:                1,
		"forgemesh_gateway_stream_first_byte_seconds_count":                      1,
		`forgemesh_gateway_instances{state="healthy"}`:                           1,
		`forgemesh_gateway_http_request_duration_seconds_count{route="metrics"}`: 1,
	})
	for _, secret := range []string{"tenant-a", "svc-a", "alice", routerKeyTenantA, "127.0.0.1", "/api/v1"} {
		if strings.Contains(text, secret) {
			t.Errorf("指标输出不应包含 %q", secret)
		}
	}
}

// TestMetrics_CountsRateLimitedRequests 验证被限流拒绝的请求计入 rate_limited_total 与 429。
func TestMetrics_CountsRateLimitedRequests(t *testing.T) {
	_, upstreamURL := newRecordingUpstream(t)
	cfg := apiKeyConfig(t, upstreamURL)
	cfg.RateLimitRPS, cfg.RateLimitBurst = 0.0001, 1
	r, err := New(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	for i, want := range []int{http.StatusOK, http.StatusTooManyRequests} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
		req.Header.Set("Authorization", "Bearer "+routerKeyTenantA)
		r.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Fatalf("第 %d 个请求应 %d，实际 %d", i+1, want, rec.Code)
		}
	}

	assertSamples(t, metricstest.MustParse(t, scrapeText(t, r)), map[string]float64{
		"forgemesh_gateway_rate_limited_total":        1,
		requestsKey(metrics.RouteControlPlane, "429"): 1,
	})
}

// TestMetrics_CountsControlPlaneErrors 验证资源接口反代连不上控制平面时计一次上游错误。
func TestMetrics_CountsControlPlaneErrors(t *testing.T) {
	r := newTestRouter(t, "http://127.0.0.1:1")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("控制平面不可达应 502，实际 %d", rec.Code)
	}

	assertSamples(t, metricstest.MustParse(t, scrapeText(t, r)), map[string]float64{
		`forgemesh_gateway_upstream_errors_total{upstream="control_plane",kind="connect"}`: 1,
		requestsKey(metrics.RouteControlPlane, "502"):                                      1,
	})
}
