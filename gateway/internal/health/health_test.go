package health

import (
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// mockDialSuccess 模拟上游可达：返回一个可关闭的连接。
func mockDialSuccess(network, address string, timeout time.Duration) (net.Conn, error) {
	a, b := net.Pipe()
	_ = b.Close() // 关闭不需要的一端，避免泄漏。
	return a, nil
}

// mockDialFailure 模拟上游不可达：始终返回错误。
func mockDialFailure(network, address string, timeout time.Duration) (net.Conn, error) {
	return nil, errors.New("connection refused")
}

// TestLive_AlwaysOK 验证存活探针始终返回 200 {"status":"ok"}。
func TestLive_AlwaysOK(t *testing.T) {
	c := New("http://upstream:8080")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/healthz", nil)
	c.Live(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("存活探针应返回 200，实际 %d", rec.Code)
	}
	if got := rec.Body.String(); got != `{"status":"ok"}` {
		t.Fatalf("存活探针响应体应为 {\"status\":\"ok\"}，实际 %q", got)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json; charset=utf-8" {
		t.Fatalf("Content-Type 应为 application/json; charset=utf-8，实际 %q", ct)
	}
}

// TestReady_UpstreamReachable 验证上游可达时返回 200 {"status":"ready"}。
func TestReady_UpstreamReachable(t *testing.T) {
	c := New("http://upstream:8080")
	c.dial = mockDialSuccess // 注入 mock dial

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("上游可达时应返回 200，实际 %d", rec.Code)
	}
	if got := rec.Body.String(); got != `{"status":"ready"}` {
		t.Fatalf("就绪探针响应体应为 {\"status\":\"ready\"}，实际 %q", got)
	}
}

// TestReady_UpstreamUnreachable 验证上游不可达时返回 503。
func TestReady_UpstreamUnreachable(t *testing.T) {
	c := New("http://upstream:8080")
	c.dial = mockDialFailure // 注入 mock dial

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("上游不可达时应返回 503，实际 %d", rec.Code)
	}
	if got := rec.Body.String(); got != `{"status":"unavailable","reason":"upstream unreachable"}` {
		t.Fatalf("不可达响应体不符，实际 %q", got)
	}
}

// TestReady_InvalidUpstream 验证 upstreamURL 为空时返回 503。
func TestReady_InvalidUpstream(t *testing.T) {
	c := New("") // 空 URL → upstream 为 nil 或 Host 为空

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("无效上游时应返回 503，实际 %d", rec.Code)
	}
	if got := rec.Body.String(); got != `{"status":"unavailable","reason":"invalid upstream"}` {
		t.Fatalf("无效上游响应体不符，实际 %q", got)
	}
}

// TestReady_DefaultPortHTTP 验证 HTTP scheme 缺省端口补全为 80。
func TestReady_DefaultPortHTTP(t *testing.T) {
	c := New("http://upstream") // 无显式端口
	c.dial = mockDialSuccess

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("HTTP 缺省 80 端口应可达(200)，实际 %d", rec.Code)
	}
}

// TestReady_DefaultPortHTTPS 验证 HTTPS scheme 缺省端口补全为 443。
func TestReady_DefaultPortHTTPS(t *testing.T) {
	c := New("https://upstream") // 无显式端口
	c.dial = mockDialSuccess

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("HTTPS 缺省 443 端口应可达(200)，实际 %d", rec.Code)
	}
}

// TestReady_NilDialFallback 验证 dial 为 nil 时回退到 net.DialTimeout（对不可达地址返回 503）。
func TestReady_NilDialFallback(t *testing.T) {
	c := New("http://127.0.0.1:1")     // 端口 1 通常无服务
	c.dial = nil                       // 强制走回退路径
	c.timeout = 100 * time.Millisecond // 缩短超时，避免测试卡顿

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	c.Ready(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("nil dial 回退后不可达地址应 503，实际 %d", rec.Code)
	}
}
