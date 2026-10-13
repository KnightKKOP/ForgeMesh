package upstream

import (
	"bytes"
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/middleware"
)

const (
	testTraceID     = "4bf92f3577b34da6a3ce929d0e0e4736"
	testTraceparent = "00-" + testTraceID + "-00f067aa0ba902b7-01"
)

// tracedContext 返回经 middleware.TraceContext 处理、携带 traceparent 的请求 context。
func tracedContext(t *testing.T, traceparent string) context.Context {
	t.Helper()
	var ctx context.Context
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
	req.Header.Set(middleware.TraceparentHeader, traceparent)
	middleware.TraceContext()(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		ctx = r.Context()
	})).ServeHTTP(httptest.NewRecorder(), req)
	return ctx
}

// headerLog 记录模拟控制平面收到的请求头。
type headerLog struct {
	mu      sync.Mutex
	headers []http.Header
}

func (h *headerLog) record(r *http.Request) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.headers = append(h.headers, r.Header.Clone())
}

func (h *headerLog) all() []http.Header {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.headers
}

// TestResolve_ForwardsTraceparent 验证 Resolver 查询控制平面时附带请求的 traceparent；
// context 中没有追踪上下文时不带。
func TestResolve_ForwardsTraceparent(t *testing.T) {
	var got headerLog
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		got.record(r)
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Minute)

	if _, err := r.Resolve(tracedContext(t, testTraceparent), svcA, "tenant-a"); err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if _, err := r.Resolve(context.Background(), ServiceRef{Name: "svc-b"}, "tenant-a"); err != nil {
		t.Fatalf("解析失败: %v", err)
	}

	headers := got.all()
	if len(headers) != 2 {
		t.Fatalf("控制平面应收到 2 次查询，实际 %d", len(headers))
	}
	if tp := headers[0].Values(middleware.TraceparentHeader); len(tp) != 1 || tp[0] != testTraceparent {
		t.Fatalf("查询应附带请求的 traceparent，实际 %v", tp)
	}
	if tp := headers[1].Get(middleware.TraceparentHeader); tp != "" {
		t.Fatalf("没有追踪上下文时不应附带 traceparent，实际 %q", tp)
	}
}

// TestClient_ForwardsTraceHeaders 验证资源接口反代把 traceparent 与 tracestate 转发给控制平面。
func TestClient_ForwardsTraceHeaders(t *testing.T) {
	var got headerLog
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got.record(r)
	}))
	defer cp.Close()
	target, err := url.Parse(cp.URL)
	if err != nil {
		t.Fatal(err)
	}
	c := New(target, "X-Correlation-ID", "X-Tenant-ID", log.New(io.Discard, "", 0))

	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(middleware.TraceparentHeader, testTraceparent)
	req.Header.Set(middleware.TracestateHeader, "vendor=opaque")
	c.ServeHTTP(httptest.NewRecorder(), req)

	headers := got.all()
	if len(headers) != 1 {
		t.Fatalf("控制平面应收到 1 个请求，实际 %d", len(headers))
	}
	if tp := headers[0].Get(middleware.TraceparentHeader); tp != testTraceparent {
		t.Fatalf("traceparent 应转发给控制平面，实际 %q", tp)
	}
	if ts := headers[0].Get(middleware.TracestateHeader); ts != "vendor=opaque" {
		t.Fatalf("tracestate 应转发给控制平面，实际 %q", ts)
	}
}

// TestClient_ErrorLogCarriesCorrelationAndTraceIDs 验证控制平面不可达时的错误日志带关联 ID 与
// trace-id。
func TestClient_ErrorLogCarriesCorrelationAndTraceIDs(t *testing.T) {
	target, err := url.Parse("http://127.0.0.1:1")
	if err != nil {
		t.Fatal(err)
	}
	var logs bytes.Buffer
	c := New(target, "X-Correlation-ID", "X-Tenant-ID", log.New(&logs, "", 0))
	h := middleware.Chain(c, middleware.Correlation("X-Correlation-ID"), middleware.TraceContext())

	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("X-Correlation-ID", "corr-1")
	req.Header.Set(middleware.TraceparentHeader, testTraceparent)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("控制平面不可达应 502，实际 %d", rec.Code)
	}
	if want := "corr=corr-1 trace=" + testTraceID + " "; !strings.Contains(logs.String(), want) {
		t.Fatalf("错误日志应含 %q，实际 %q", want, logs.String())
	}
}
