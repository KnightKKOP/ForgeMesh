package stream

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// TestSingleJoiningSlash 验证路径拼接在连接处恰好保留一个斜杠。
func TestSingleJoiningSlash(t *testing.T) {
	cases := []struct {
		name string
		a    string
		b    string
		want string
	}{
		{"双斜杠合并", "/api/", "/v1/resources", "/api/v1/resources"},
		{"无斜杠补一个", "/api", "v1/resources", "/api/v1/resources"},
		{"a 尾斜杠 b 无前斜杠", "/api/", "v1", "/api/v1"},
		{"a 无尾斜杠 b 有前斜杠", "/api", "/v1", "/api/v1"},
		{"a 为空", "", "/v1", "/v1"},
		{"a 为空 b 无斜杠", "", "v1", "v1"},
		{"两者均为空", "", "", ""},
		{"根路径拼接", "/", "/", "/"},
		{"a 无尾斜杠 b 无前斜杠且 b 为空", "/api", "", "/api/"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := singleJoiningSlash(tc.a, tc.b)
			if got != tc.want {
				t.Fatalf("singleJoiningSlash(%q, %q) = %q, want %q", tc.a, tc.b, got, tc.want)
			}
		})
	}
}

// TestIsHopByHop 验证逐跳头识别（大小写不敏感）。
func TestIsHopByHop(t *testing.T) {
	hopByHop := []string{
		"Connection", "connection", "CONNECTION",
		"Proxy-Connection", "proxy-connection",
		"Keep-Alive", "keep-alive",
		"Proxy-Authenticate", "proxy-authenticate",
		"Proxy-Authorization", "proxy-authorization",
		"TE", "te", "Trailer", "trailer",
		"Transfer-Encoding", "transfer-encoding",
		"Upgrade", "upgrade",
	}
	for _, h := range hopByHop {
		if !isHopByHop(h) {
			t.Errorf("isHopByHop(%q) = false, want true", h)
		}
	}

	nonHopByHop := []string{
		"Content-Type", "Authorization", "X-Tenant-ID",
		"X-Correlation-ID", "Accept", "Cache-Control",
		"X-Accel-Buffering", "Host", "Content-Length",
	}
	for _, h := range nonHopByHop {
		if isHopByHop(h) {
			t.Errorf("isHopByHop(%q) = true, want false", h)
		}
	}
}

// TestCopyForwardHeaders 验证转发头复制：剔除逐跳头、Host、Content-Length，保留其余。
func TestCopyForwardHeaders(t *testing.T) {
	src := http.Header{}
	src.Set("Content-Type", "application/json")
	src.Set("Authorization", "Bearer token")
	src.Set("X-Tenant-ID", "tenant-a")
	src.Set("X-Correlation-ID", "corr-123")
	src.Set("Accept", "text/event-stream")
	// 应被剔除的头。
	src.Set("Connection", "keep-alive")
	src.Set("Proxy-Connection", "keep-alive")
	src.Set("Keep-Alive", "timeout=30")
	src.Set("Transfer-Encoding", "chunked")
	src.Set("Upgrade", "h2c")
	src.Set("Host", "upstream:8080")
	src.Set("Content-Length", "42")

	dst := copyForwardHeaders(src)

	// 验证保留的头。
	mustKeep := []string{"Content-Type", "Authorization", "X-Tenant-ID", "X-Correlation-ID", "Accept"}
	for _, h := range mustKeep {
		if dst.Get(h) == "" {
			t.Errorf("头 %q 应被保留，但缺失", h)
		}
	}

	// 验证剔除的头（逐跳头 + Host + Content-Length）。
	mustDrop := []string{
		"Connection", "Proxy-Connection", "Keep-Alive",
		"Transfer-Encoding", "Upgrade", "Host", "Content-Length",
	}
	for _, h := range mustDrop {
		if dst.Get(h) != "" {
			t.Errorf("头 %q 应被剔除，但存在值 %q", h, dst.Get(h))
		}
	}
}

// TestCopyForwardHeaders_Empty 验证空头集返回空头集。
func TestCopyForwardHeaders_Empty(t *testing.T) {
	dst := copyForwardHeaders(http.Header{})
	if len(dst) != 0 {
		t.Fatalf("空头集应返回空，实际 %d 个头", len(dst))
	}
}

// TestCopyForwardHeaders_DoesNotMutateSource 验证不修改源头集合。
func TestCopyForwardHeaders_DoesNotMutateSource(t *testing.T) {
	src := http.Header{}
	src.Set("Content-Type", "application/json")
	src.Set("Connection", "keep-alive")

	_ = copyForwardHeaders(src)

	if src.Get("Content-Type") != "application/json" {
		t.Error("源头 Content-Type 被修改")
	}
	if src.Get("Connection") != "keep-alive" {
		t.Error("源头 Connection 被修改")
	}
}

// TestProxy_SSEForwarding 验证 SSE 流式转发：mock upstream 返回 SSE 数据，proxy 应原样转发。
func TestProxy_SSEForwarding(t *testing.T) {
	// 启动 mock upstream，返回 SSE 数据。
	sseData := "data: hello\n\n" + "data: world\n\n"
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(sseData))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	proxy := New(upstreamURL, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/chat/stream", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("SSE 转发应返回 200，实际 %d", rec.Code)
	}
	if got := rec.Body.String(); got != sseData {
		t.Fatalf("SSE 数据应原样转发，\nwant %q\ngot  %q", sseData, got)
	}
	// 验证流式语义头被强制设置。
	if ct := rec.Header().Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type 应为 text/event-stream，实际 %q", ct)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "no-cache" {
		t.Fatalf("Cache-Control 应为 no-cache，实际 %q", cc)
	}
	if xab := rec.Header().Get("X-Accel-Buffering"); xab != "no" {
		t.Fatalf("X-Accel-Buffering 应为 no，实际 %q", xab)
	}
}

// TestProxy_HopByHopHeadersFiltered 验证上游响应中的 hop-by-hop 头被过滤不转发给客户端。
func TestProxy_HopByHopHeadersFiltered(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		// 设置 hop-by-hop 头，不应被转发。
		w.Header().Set("Connection", "keep-alive")
		w.Header().Set("Transfer-Encoding", "chunked")
		w.Header().Set("Upgrade", "h2c")
		// 设置正常头，应被转发。
		w.Header().Set("X-Custom-Header", "custom-value")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: test\n\n"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	proxy := New(upstreamURL, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/stream", nil)
	proxy.ServeHTTP(rec, req)

	// hop-by-hop 头不应出现在客户端响应中。
	hopHeaders := []string{"Connection", "Transfer-Encoding", "Upgrade"}
	for _, h := range hopHeaders {
		// Connection 头会被 proxy 强制设为 keep-alive，所以特殊处理：
		// 只检查 Transfer-Encoding 和 Upgrade 不存在。
		if h == "Connection" {
			continue
		}
		if v := rec.Header().Get(h); v != "" {
			t.Errorf("hop-by-hop 头 %q 不应转发，实际存在 %q", h, v)
		}
	}
	// 正常头应被转发。
	if v := rec.Header().Get("X-Custom-Header"); v != "custom-value" {
		t.Fatalf("X-Custom-Header 应转发为 custom-value，实际 %q", v)
	}
}

// TestProxy_UpstreamError 验证上游不可达时返回 502 Bad Gateway。
func TestProxy_UpstreamError(t *testing.T) {
	// 指向一个不存在的端口。
	upstreamURL, _ := url.Parse("http://127.0.0.1:1")
	proxy := New(upstreamURL, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/stream", nil)
	// 设置较短超时以加速测试。
	proxy.client.Timeout = 200 * time.Millisecond
	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("上游不可达应返回 502，实际 %d", rec.Code)
	}
}

// TestProxy_ForwardsClientHeaders 验证客户端有效头被转发到上游（剔除 hop-by-hop）。
func TestProxy_ForwardsClientHeaders(t *testing.T) {
	var receivedAuth, receivedTenant string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedAuth = r.Header.Get("Authorization")
		receivedTenant = r.Header.Get("X-Tenant-ID")
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "data: ok\n\n")
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	proxy := New(upstreamURL, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/stream", nil)
	req.Header.Set("Authorization", "Bearer my-token")
	req.Header.Set("X-Tenant-ID", "tenant-a")
	proxy.ServeHTTP(rec, req)

	if receivedAuth != "Bearer my-token" {
		t.Fatalf("Authorization 头应转发，实际 %q", receivedAuth)
	}
	if receivedTenant != "tenant-a" {
		t.Fatalf("X-Tenant-ID 头应转发，实际 %q", receivedTenant)
	}
}

// TestBuildUpstreamURL 验证上游 URL 拼接逻辑。
func TestBuildUpstreamURL(t *testing.T) {
	cases := []struct {
		name     string
		upstream string
		reqPath  string
		reqQuery string
		want     string
	}{
		{
			name:     "基础拼接",
			upstream: "http://upstream:8080",
			reqPath:  "/v1/resources",
			want:     "http://upstream:8080/v1/resources",
		},
		{
			name:     "带 query 透传",
			upstream: "http://upstream:8080",
			reqPath:  "/v1/resources",
			reqQuery: "limit=10&offset=0",
			want:     "http://upstream:8080/v1/resources?limit=10&offset=0",
		},
		{
			name:     "upstream 带路径前缀",
			upstream: "http://upstream:8080/api",
			reqPath:  "/v1/resources",
			want:     "http://upstream:8080/api/v1/resources",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, _ := url.Parse(tc.upstream)
			proxy := New(u, nil)
			in, _ := url.Parse(tc.reqPath)
			if tc.reqQuery != "" {
				in.RawQuery = tc.reqQuery
			}
			got := proxy.buildUpstreamURL(in)
			if got != tc.want {
				t.Fatalf("buildUpstreamURL = %q, want %q", got, tc.want)
			}
		})
	}
}

// 确保 strings 包被使用（在 buildUpstreamURL 间接使用，此处显式引用避免未使用导入）。
var _ = strings.HasPrefix
