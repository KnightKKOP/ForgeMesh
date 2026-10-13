package stream

import (
	"bytes"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/middleware"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/upstream"
)

const (
	testTraceID     = "4bf92f3577b34da6a3ce929d0e0e4736"
	testTraceparent = "00-" + testTraceID + "-00f067aa0ba902b7-01"
)

// TestProxy_ForwardsTraceHeadersToInstance 验证 traceparent 与 tracestate 随推理请求转发给实例。
func TestProxy_ForwardsTraceHeadersToInstance(t *testing.T) {
	var mu sync.Mutex
	var got http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		got = r.Header.Clone()
		mu.Unlock()
		_, _ = io.WriteString(w, "data: ok\n\n")
	}))
	defer srv.Close()

	req := newChatRequest("svc-a")
	req.Header.Set(middleware.TraceparentHeader, testTraceparent)
	req.Header.Set(middleware.TracestateHeader, "vendor=opaque")
	New(newFakeResolver([]string{srv.URL}, nil), "X-Tenant-ID", allowLoopback, nil).
		ServeHTTP(httptest.NewRecorder(), req)

	mu.Lock()
	defer mu.Unlock()
	if tp := got.Get(middleware.TraceparentHeader); tp != testTraceparent {
		t.Fatalf("traceparent 应转发给实例，实际 %q", tp)
	}
	if ts := got.Get(middleware.TracestateHeader); ts != "vendor=opaque" {
		t.Fatalf("tracestate 应转发给实例，实际 %q", ts)
	}
}

// TestProxy_ErrorsCarryCorrelationAndTraceIDs 验证代理自己返回的错误信封与错误日志带关联 ID
// 与 trace-id（关联 ID 取自 context，自定义的关联 ID 请求头同样适用）。
func TestProxy_ErrorsCarryCorrelationAndTraceIDs(t *testing.T) {
	var logs bytes.Buffer
	proxy := New(newFakeResolver(nil, upstream.ErrServiceNotFound), "X-Tenant-ID", allowLoopback,
		log.New(&logs, "", 0))
	h := middleware.Chain(proxy, middleware.Correlation("X-Request-ID"), middleware.TraceContext())

	req := newChatRequest("svc-a")
	req.Header.Set("X-Request-ID", "corr-1")
	req.Header.Set(middleware.TraceparentHeader, testTraceparent)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("服务不存在应 503，实际 %d", rec.Code)
	}
	var body struct {
		Error struct {
			Code          string `json:"code"`
			CorrelationID string `json:"correlationId"`
			TraceID       string `json:"traceId"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("错误信封不是 JSON: %v", err)
	}
	if body.Error.Code != "NO_HEALTHY_UPSTREAM" || body.Error.CorrelationID != "corr-1" ||
		body.Error.TraceID != testTraceID {
		t.Fatalf("错误信封应带错误码、关联 ID 与 trace-id，实际 %+v", body.Error)
	}
	if want := "corr=corr-1 trace=" + testTraceID; !strings.Contains(logs.String(), want) {
		t.Fatalf("错误日志应含 %q，实际 %q", want, logs.String())
	}
}
