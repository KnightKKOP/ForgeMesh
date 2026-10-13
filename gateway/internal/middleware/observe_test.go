package middleware

import (
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics/metricstest"
)

// scrapeMetrics 抓取 m 的输出并按文本格式严格解析。
func scrapeMetrics(t *testing.T, m *metrics.Gateway) map[string]float64 {
	t.Helper()
	rec := httptest.NewRecorder()
	m.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return metricstest.MustParse(t, rec.Body.String())
}

func requestsKey(route metrics.Route, status string) string {
	return `forgemesh_gateway_http_requests_total{route="` + string(route) + `",status="` + status + `"}`
}

func classifyAs(route metrics.Route) func(*http.Request) metrics.Route {
	return func(*http.Request) metrics.Route { return route }
}

// TestObserve_RecordsRouteClassAndStatus 验证按路由类别与最终状态码计数并记录耗时：
// 什么都没写或只写响应体为 200；先 Flush 后 WriteHeader 时状态码已是 200。
func TestObserve_RecordsRouteClassAndStatus(t *testing.T) {
	handlers := map[string]http.HandlerFunc{
		"什么都没写": func(http.ResponseWriter, *http.Request) {},
		"只写响应体": func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "ok") },
		"404":   func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotFound) },
		"500": func(w http.ResponseWriter, _ *http.Request) {
			http.Error(w, "boom", http.StatusInternalServerError)
		},
		"先 Flush": func(w http.ResponseWriter, _ *http.Request) {
			w.(http.Flusher).Flush()
			w.WriteHeader(http.StatusBadGateway)
		},
	}
	m := metrics.NewGateway()
	for name, h := range handlers {
		rec := httptest.NewRecorder()
		Observe(m, classifyAs(metrics.RouteControlPlane))(h).ServeHTTP(rec,
			httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))
		if name == "404" && rec.Code != http.StatusNotFound {
			t.Fatalf("Observe 不应改变响应，实际 %d", rec.Code)
		}
	}

	samples := scrapeMetrics(t, m)
	want := map[string]float64{
		requestsKey(metrics.RouteControlPlane, "200"):                                  3,
		requestsKey(metrics.RouteControlPlane, "404"):                                  1,
		requestsKey(metrics.RouteControlPlane, "500"):                                  1,
		`forgemesh_gateway_http_request_duration_seconds_count{route="control_plane"}`: 5,
	}
	for key, value := range want {
		if got, ok := samples[key]; !ok || got != value {
			t.Errorf("%s = %v（存在 %v），want %v", key, got, ok, value)
		}
	}
	if _, ok := samples[requestsKey(metrics.RouteControlPlane, "502")]; ok {
		t.Error("Flush 之后的 WriteHeader 不再生效，不应计为 502")
	}
}

// TestObserve_IgnoresInformationalStatus 验证 1xx 信息响应之后记录最终状态码。
func TestObserve_IgnoresInformationalStatus(t *testing.T) {
	m := metrics.NewGateway()
	srv := httptest.NewServer(Observe(m, classifyAs(metrics.RouteInference))(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusEarlyHints)
			w.WriteHeader(http.StatusCreated)
		})))
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("客户端应收到 201，实际 %d", resp.StatusCode)
	}

	samples := scrapeMetrics(t, m)
	if got := samples[requestsKey(metrics.RouteInference, "201")]; got != 1 {
		t.Fatalf("应记录最终状态码 201，实际 %v（%v）", got, samples)
	}
	if _, ok := samples[requestsKey(metrics.RouteInference, "103")]; ok {
		t.Fatal("不应记录 1xx 信息响应")
	}
}

// TestObserve_KeepsFlusherAndUnwrap 验证包装后的 ResponseWriter 仍能 Flush（流式代理依赖），
// 且 Unwrap 返回底层 ResponseWriter。
func TestObserve_KeepsFlusherAndUnwrap(t *testing.T) {
	rec := httptest.NewRecorder()
	var unwrapped http.ResponseWriter
	h := Observe(nil, classifyAs(metrics.RouteInference))(
		http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			if err := http.NewResponseController(w).Flush(); err != nil {
				t.Errorf("Flush 失败: %v", err)
			}
			if u, ok := w.(interface{ Unwrap() http.ResponseWriter }); ok {
				unwrapped = u.Unwrap()
			}
		}))
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc/chat", nil))

	if !rec.Flushed {
		t.Fatal("Flush 应传到底层 ResponseWriter")
	}
	if unwrapped != rec {
		t.Fatalf("Unwrap 应返回底层 ResponseWriter，实际 %T", unwrapped)
	}
}

// TestObserve_CountsRecoveredPanicAs500 验证 Observe 位于 Recovery 外层时，panic 兜底返回的
// 500 被计入。
func TestObserve_CountsRecoveredPanicAs500(t *testing.T) {
	m := metrics.NewGateway()
	h := Chain(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { panic("boom") }),
		Observe(m, classifyAs(metrics.RouteControlPlane)),
		Recovery(log.New(io.Discard, "", 0)),
	)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("panic 应兜底为 500，实际 %d", rec.Code)
	}
	if got := scrapeMetrics(t, m)[requestsKey(metrics.RouteControlPlane, "500")]; got != 1 {
		t.Fatalf("兜底的 500 应计入，实际 %v", got)
	}
}
