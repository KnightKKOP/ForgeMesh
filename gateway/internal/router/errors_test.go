// errors_test.go 从网关入口验证错误响应：
//
//   - 网关自己返回的错误都是 JSON 错误信封 {"error": {"code", "message", "correlationId", "traceId"}}
//     （Content-Type: application/json，带 nosniff），信封中的关联 ID 与响应头一致、trace-id 与
//     请求的 traceparent 一致，访问日志带同一组 ID；/metrics 不经过 Correlation 与 TraceContext，
//     它的 405 不带 ID；
//   - 控制平面与推理实例自己的错误响应原样透传。
package router

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/middleware"
)

// gatewayError 是错误信封中 error 对象的字段。
type gatewayError struct {
	Code          string `json:"code"`
	Message       string `json:"message"`
	CorrelationID string `json:"correlationId"`
	TraceID       string `json:"traceId"`
}

// readGatewayError 解析错误信封（不允许信封之外的字段），并检查 Content-Type 与 nosniff。
func readGatewayError(t *testing.T, rec *httptest.ResponseRecorder) gatewayError {
	t.Helper()
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type 应为 application/json，实际 %q（body=%s）", ct, rec.Body.String())
	}
	if v := rec.Header().Get("X-Content-Type-Options"); v != "nosniff" {
		t.Fatalf("应带 X-Content-Type-Options: nosniff，实际 %q", v)
	}
	var body struct {
		Error *gatewayError `json:"error"`
	}
	dec := json.NewDecoder(bytes.NewReader(rec.Body.Bytes()))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&body); err != nil || body.Error == nil {
		t.Fatalf("响应体不是错误信封: %v（body=%s）", err, rec.Body.String())
	}
	return *body.Error
}

// TestGatewayErrors_UseEnvelopeWithIDs 逐一验证网关自己返回的错误：状态码、Content-Type、错误码，
// 信封与访问日志中的关联 ID（与响应头一致）与 trace-id（与请求的 traceparent 一致）；429 带
// Retry-After。
func TestGatewayErrors_UseEnvelopeWithIDs(t *testing.T) {
	// 控制平面把任何服务都解析到不可达的实例（测试配置允许回环地址）。
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, readyEndpointsJSON("Ready", []string{"http://127.0.0.1:1"}))
	}))
	t.Cleanup(cp.Close)

	cases := []struct {
		name         string
		upstreamURL  string
		rateLimited  bool // 每个租户只有 1 个令牌、几乎不补充
		method, path string
		key          string
		status       int
		code         string
	}{
		{"限流", cp.URL, true, http.MethodGet, "/api/v1/resources", routerKeyTenantA,
			http.StatusTooManyRequests, "RATE_LIMITED"},
		{"控制平面不可达", "http://127.0.0.1:1", false, http.MethodGet, "/api/v1/resources", routerKeyTenantA,
			http.StatusBadGateway, "UPSTREAM_UNAVAILABLE"},
		{"推理实例不可达", cp.URL, false, http.MethodPost, "/api/v1/inference/svc-a/chat", routerKeyTenantA,
			http.StatusBadGateway, "UPSTREAM_UNAVAILABLE"},
		{"/api/v1/ 下的未知路径", cp.URL, false, http.MethodGet, "/api/v1/unknown", routerKeyTenantA,
			http.StatusNotFound, "ROUTE_NOT_FOUND"},
		{"其他未知路径（不需要凭证）", cp.URL, false, http.MethodGet, "/unknown", "",
			http.StatusNotFound, "ROUTE_NOT_FOUND"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := apiKeyConfig(t, tc.upstreamURL)
			if tc.rateLimited {
				cfg.RateLimitRPS, cfg.RateLimitBurst = 0.0001, 1
			}
			var logs syncBuffer
			r, err := New(cfg, log.New(&logs, "", 0))
			if err != nil {
				t.Fatalf("New 失败: %v", err)
			}
			do := func() *httptest.ResponseRecorder {
				req := httptest.NewRequest(tc.method, tc.path, nil)
				if tc.key != "" {
					req.Header.Set("Authorization", "Bearer "+tc.key)
				}
				req.Header.Set(middleware.TraceparentHeader, testTraceparent)
				rec := httptest.NewRecorder()
				r.ServeHTTP(rec, req)
				return rec
			}
			if tc.rateLimited {
				if rec := do(); rec.Code != http.StatusOK {
					t.Fatalf("第一个请求应 200，实际 %d", rec.Code)
				}
			}
			rec := do()

			if rec.Code != tc.status {
				t.Fatalf("应 %d，实际 %d（body=%s）", tc.status, rec.Code, rec.Body.String())
			}
			got := readGatewayError(t, rec)
			corr := rec.Header().Get("X-Correlation-ID")
			if got.Code != tc.code || got.Message == "" || corr == "" || got.CorrelationID != corr ||
				got.TraceID != testTraceID {
				t.Fatalf("错误信封应带 %s、关联 ID %q 与 trace-id，实际 %+v", tc.code, corr, got)
			}
			if tc.status == http.StatusTooManyRequests {
				if secs, err := strconv.Atoi(rec.Header().Get("Retry-After")); err != nil || secs < 1 {
					t.Fatalf("429 应带正整数秒的 Retry-After，实际 %q", rec.Header().Get("Retry-After"))
				}
			}
			want := " status=" + strconv.Itoa(tc.status) + " "
			found := false
			for _, line := range strings.Split(logs.String(), "\n") {
				if strings.HasPrefix(line, "access ") && strings.HasSuffix(line, " corr="+corr+" trace="+testTraceID) {
					found = strings.Contains(line, want)
				}
			}
			if !found {
				t.Fatalf("访问日志应有一行%s且带同一组 ID，实际 %q", want, logs.String())
			}
		})
	}
}

// TestGatewayErrors_MetricsMethodNotAllowed 验证 /metrics 的其他方法返回 405 METHOD_NOT_ALLOWED
// 错误信封与 Allow；/metrics 不经过 Correlation 与 TraceContext，信封与响应头都没有关联 ID。
func TestGatewayErrors_MetricsMethodNotAllowed(t *testing.T) {
	r := newAPIKeyRouter(t, "http://127.0.0.1:1")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/metrics", nil)
	req.Header.Set(middleware.TraceparentHeader, testTraceparent)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed || rec.Header().Get("Allow") != "GET, HEAD" {
		t.Fatalf("应 405 且 Allow 为 GET, HEAD，实际 %d %q", rec.Code, rec.Header().Get("Allow"))
	}
	if got, want := readGatewayError(t, rec), (gatewayError{Code: "METHOD_NOT_ALLOWED", Message: "method not allowed"}); got != want {
		t.Fatalf("错误信封应为 %+v，实际 %+v", want, got)
	}
	if corr := rec.Header().Get("X-Correlation-ID"); corr != "" {
		t.Fatalf("/metrics 的响应不应带关联 ID，实际 %q", corr)
	}
}

// TestGatewayErrors_UpstreamErrorResponsesPassThrough 验证控制平面与推理实例自己的错误响应
// （状态码、Content-Type、响应体）原样透传，不被改写为网关的错误信封。
func TestGatewayErrors_UpstreamErrorResponsesPassThrough(t *testing.T) {
	instance := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = io.WriteString(w, "instance failure")
	}))
	t.Cleanup(instance.Close)
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/inference-services/svc-a" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, readyEndpointsJSON("Ready", []string{instance.URL}))
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, "control plane maintenance")
	}))
	t.Cleanup(cp.Close)
	r := newAPIKeyRouter(t, cp.URL)

	for _, c := range []struct {
		method, path string
		status       int
		body         string
	}{
		{http.MethodGet, "/api/v1/resources", http.StatusServiceUnavailable, "control plane maintenance"},
		{http.MethodPost, "/api/v1/inference/svc-a/chat", http.StatusInternalServerError, "instance failure"},
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(c.method, c.path, nil)
		req.Header.Set("Authorization", "Bearer "+routerKeyTenantA)
		r.ServeHTTP(rec, req)

		if ct := rec.Header().Get("Content-Type"); rec.Code != c.status || ct != "text/plain; charset=utf-8" ||
			rec.Body.String() != c.body {
			t.Fatalf("%s 的错误响应应原样透传（%d %q），实际 %d %q %q", c.path, c.status, c.body,
				rec.Code, ct, rec.Body.String())
		}
	}
}
