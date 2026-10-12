package health

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// newControlPlane 启动模拟控制平面：GET /readyz 返回 status。
func newControlPlane(t *testing.T, status int) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

// probe 调用就绪探针，返回状态码与响应体。
func probe(c *Checker) (int, string) {
	rec := httptest.NewRecorder()
	c.Ready(rec, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	return rec.Code, rec.Body.String()
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

// TestReady_UpstreamReady 验证控制平面 /readyz 返回 200 时就绪探针返回 200 {"status":"ready"}。
func TestReady_UpstreamReady(t *testing.T) {
	srv := newControlPlane(t, http.StatusOK)

	code, body := probe(New(srv.URL))
	if code != http.StatusOK {
		t.Fatalf("控制平面就绪时应返回 200，实际 %d", code)
	}
	if body != `{"status":"ready"}` {
		t.Fatalf("就绪探针响应体应为 {\"status\":\"ready\"}，实际 %q", body)
	}
}

// TestReady_UpstreamNotReady 验证控制平面在线但未就绪（如权威存储不可用）时返回 503。
func TestReady_UpstreamNotReady(t *testing.T) {
	srv := newControlPlane(t, http.StatusServiceUnavailable)

	code, body := probe(New(srv.URL))
	if code != http.StatusServiceUnavailable {
		t.Fatalf("控制平面未就绪时应返回 503，实际 %d", code)
	}
	if body != `{"status":"unavailable","reason":"upstream not ready"}` {
		t.Fatalf("未就绪响应体不符，实际 %q", body)
	}
}

// TestReady_UpstreamUnreachable 验证控制平面不可达时返回 503。
func TestReady_UpstreamUnreachable(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	srv.Close()

	code, body := probe(New(srv.URL))
	if code != http.StatusServiceUnavailable {
		t.Fatalf("控制平面不可达时应返回 503，实际 %d", code)
	}
	if body != `{"status":"unavailable","reason":"upstream unreachable"}` {
		t.Fatalf("不可达响应体不符，实际 %q", body)
	}
}

// TestReady_UpstreamTimeout 验证控制平面迟迟不响应时按不可达处理。
func TestReady_UpstreamTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-time.After(5 * time.Second):
		}
	}))
	t.Cleanup(srv.Close)
	c := New(srv.URL)
	c.client.Timeout = 50 * time.Millisecond

	code, body := probe(c)
	if code != http.StatusServiceUnavailable {
		t.Fatalf("控制平面超时应返回 503，实际 %d", code)
	}
	if body != `{"status":"unavailable","reason":"upstream unreachable"}` {
		t.Fatalf("超时响应体不符，实际 %q", body)
	}
}

// TestReady_InvalidUpstream 验证上游地址不是 http(s) 绝对地址时返回 503。
func TestReady_InvalidUpstream(t *testing.T) {
	for _, upstream := range []string{"", "control-plane:8080", "ftp://control-plane", "http://"} {
		code, body := probe(New(upstream))
		if code != http.StatusServiceUnavailable {
			t.Fatalf("上游地址 %q 非法时应返回 503，实际 %d", upstream, code)
		}
		if body != `{"status":"unavailable","reason":"invalid upstream"}` {
			t.Fatalf("上游地址 %q 非法时响应体不符，实际 %q", upstream, body)
		}
	}
}

// TestReady_KeepsUpstreamPathPrefix 验证就绪探针请求 {上游地址}/readyz，保留路径前缀。
func TestReady_KeepsUpstreamPathPrefix(t *testing.T) {
	var got string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Method + " " + r.URL.Path
	}))
	t.Cleanup(srv.Close)

	for suffix, want := range map[string]string{
		"":     "GET /readyz",
		"/":    "GET /readyz",
		"/cp":  "GET /cp/readyz",
		"/cp/": "GET /cp/readyz",
	} {
		got = ""
		if code, _ := probe(New(srv.URL + suffix)); code != http.StatusOK {
			t.Fatalf("上游地址 %q 应返回 200，实际 %d", srv.URL+suffix, code)
		}
		if got != want {
			t.Fatalf("上游地址 %q 应请求 %q，实际 %q", srv.URL+suffix, want, got)
		}
	}
}
