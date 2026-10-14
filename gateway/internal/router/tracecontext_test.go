// tracecontext_test.go 从网关入口验证追踪上下文：
//
//   - 合法的 traceparent 原样转发给控制平面（资源接口反代与 Resolver 查询）与推理实例；
//   - 缺失时由网关生成，控制平面收到的与访问日志中的 trace-id 一致；
//   - 网关自己返回的错误信封带关联 ID 与 trace-id。
package router

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
)

const (
	testTraceID     = "4bf92f3577b34da6a3ce929d0e0e4736"
	testTraceparent = "00-" + testTraceID + "-00f067aa0ba902b7-01"
)

// syncBuffer 是可并发写入的日志缓冲。
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// traceEnv 是假控制平面 + 假推理实例（apikey 模式），记录各自收到的 traceparent。
type traceEnv struct {
	router *Router
	logs   syncBuffer

	mu   sync.Mutex
	seen map[string][]string // "cp <path>" / "instance" → 收到的 traceparent
}

func newTraceEnv(t *testing.T) *traceEnv {
	t.Helper()
	env := &traceEnv{seen: make(map[string][]string)}
	instance := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		env.record("instance", r)
		_, _ = io.WriteString(w, "data: ok\n\n")
	}))
	t.Cleanup(instance.Close)
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		env.record("cp "+r.URL.Path, r)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, readyEndpointsJSON("Ready", []string{instance.URL}))
	}))
	t.Cleanup(cp.Close)
	r, err := New(apiKeyConfig(t, cp.URL), log.New(&env.logs, "", 0))
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	env.router = r
	return env
}

func (e *traceEnv) record(who string, r *http.Request) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.seen[who] = append(e.seen[who], r.Header.Values(middleware.TraceparentHeader)...)
}

func (e *traceEnv) received(who string) []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.seen[who]
}

func (e *traceEnv) do(method, path string, headers map[string]string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(method, path, nil)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	e.router.ServeHTTP(rec, req)
	return rec
}

// TestTraceContext_ForwardsInboundTraceparent 验证推理请求的 traceparent 原样带给 Resolver 对
// 控制平面的查询与推理实例，访问日志带关联 ID 与 trace-id。
func TestTraceContext_ForwardsInboundTraceparent(t *testing.T) {
	env := newTraceEnv(t)
	rec := env.do(http.MethodPost, "/api/v1/inference/svc-a/chat", map[string]string{
		"Authorization":              "Bearer " + routerKeyTenantA,
		"X-Correlation-ID":           "corr-1",
		middleware.TraceparentHeader: testTraceparent,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("推理请求应 200，实际 %d（%s）", rec.Code, rec.Body.String())
	}

	for _, who := range []string{"cp /api/v1/inference-services/svc-a", "instance"} {
		if got := env.received(who); len(got) != 1 || got[0] != testTraceparent {
			t.Errorf("%s 应收到原样的 traceparent，实际 %v", who, got)
		}
	}
	want := `access method=POST path="/api/v1/inference/svc-a/chat" status=200 `
	if logs := env.logs.String(); !strings.Contains(logs, want) ||
		!strings.Contains(logs, "corr=corr-1 trace="+testTraceID+"\n") {
		t.Fatalf("访问日志应带关联 ID 与 trace-id，实际 %q", logs)
	}
}

// TestTraceContext_GeneratesTraceparentWhenMissing 验证请求没有 traceparent 时由网关生成，
// 控制平面收到的 trace-id 与访问日志、响应的关联 ID 对得上。
func TestTraceContext_GeneratesTraceparentWhenMissing(t *testing.T) {
	env := newTraceEnv(t)
	rec := env.do(http.MethodGet, "/api/v1/inference-services", map[string]string{
		"Authorization": "Bearer " + routerKeyTenantA,
	})
	if rec.Code != http.StatusOK {
		t.Fatalf("资源请求应 200，实际 %d", rec.Code)
	}

	got := env.received("cp /api/v1/inference-services")
	if len(got) != 1 {
		t.Fatalf("控制平面应收到恰好一个 traceparent，实际 %v", got)
	}
	traceID, ok := middleware.ParseTraceparent(got[0])
	if !ok {
		t.Fatalf("网关生成的 traceparent 不合法: %q", got[0])
	}
	corr := rec.Header().Get("X-Correlation-ID")
	if want := "corr=" + corr + " trace=" + traceID + "\n"; corr == "" || !strings.Contains(env.logs.String(), want) {
		t.Fatalf("访问日志应含 %q，实际 %q", want, env.logs.String())
	}
}

// TestTraceContext_GatewayErrorsCarryIDs 验证网关自己返回的错误信封（认证失败）带关联 ID 与
// trace-id，与访问日志一致。
func TestTraceContext_GatewayErrorsCarryIDs(t *testing.T) {
	env := newTraceEnv(t)
	rec := env.do(http.MethodGet, "/api/v1/resources", map[string]string{
		middleware.TraceparentHeader: testTraceparent,
	})
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("没有凭证应 401，实际 %d", rec.Code)
	}

	var body struct {
		Error struct {
			CorrelationID string `json:"correlationId"`
			TraceID       string `json:"traceId"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("错误信封不是 JSON: %v", err)
	}
	corr := rec.Header().Get("X-Correlation-ID")
	if body.Error.CorrelationID == "" || body.Error.CorrelationID != corr || body.Error.TraceID != testTraceID {
		t.Fatalf("错误信封应带关联 ID %q 与 trace-id，实际 %+v", corr, body.Error)
	}
	if want := "status=401 "; !strings.Contains(env.logs.String(), want) {
		t.Fatalf("被拒绝的请求也应有访问日志，实际 %q", env.logs.String())
	}
	if len(env.received("cp /api/v1/resources")) != 0 {
		t.Fatal("被拒绝的请求不应触达控制平面")
	}
}
