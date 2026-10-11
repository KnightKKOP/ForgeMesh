package stream

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/upstream"
)

// fakeResolver 是测试用的解析器替身：返回固定 endpoint 列表并记录上报调用。
type fakeResolver struct {
	mu        sync.Mutex
	endpoints []string
	err       error

	failures map[string]int
	success  map[string]int
	// resolveCalls 记录 Resolve 调用参数，供断言。
	lastSvc, lastTenant string
}

func newFakeResolver(endpoints []string, err error) *fakeResolver {
	return &fakeResolver{
		endpoints: endpoints,
		err:       err,
		failures:  make(map[string]int),
		success:   make(map[string]int),
	}
}

func (f *fakeResolver) Resolve(_ context.Context, svc, tenantID string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.lastSvc, f.lastTenant = svc, tenantID
	return f.endpoints, f.err
}

func (f *fakeResolver) ReportFailure(endpoint string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.failures[endpoint]++
}

func (f *fakeResolver) ReportSuccess(endpoint string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.success[endpoint]++
}

func (f *fakeResolver) failureCount(endpoint string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.failures[endpoint]
}

func (f *fakeResolver) successCount(endpoint string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.success[endpoint]
}

// newChatRequest 构造带 {svc} 路径值的流式请求（与线上路由模式一致）。
func newChatRequest(svc string) *http.Request {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/inference/"+svc+"/chat", nil)
	req.SetPathValue("svc", svc)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	return req
}

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

// TestProxy_SSEForwarding 验证 SSE 流式转发：mock 实例返回 SSE 数据，proxy 应原样转发。
func TestProxy_SSEForwarding(t *testing.T) {
	sseData := "data: hello\n\n" + "data: world\n\n"
	upstreamSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(sseData))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer upstreamSrv.Close()

	res := newFakeResolver([]string{upstreamSrv.URL}, nil)
	proxy := New(res, "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

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
	// 完整读尽 → 应上报成功。
	if got := res.successCount(upstreamSrv.URL); got != 1 {
		t.Fatalf("应上报 1 次成功，实际 %d", got)
	}
}

// TestProxy_RoundRobin 验证轮询分发：4 次请求在 2 个实例间均匀分配，
// 且每次请求都打到当前选中实例的原始路径。
func TestProxy_RoundRobin(t *testing.T) {
	var hitsA, hitsB atomic.Int64
	var mu sync.Mutex
	var gotPaths []string
	record := func(r *http.Request, counter *atomic.Int64) {
		counter.Add(1)
		mu.Lock()
		gotPaths = append(gotPaths, r.URL.Path)
		mu.Unlock()
	}
	srvA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		record(r, &hitsA)
		_, _ = w.Write([]byte("data: a\n\n"))
	}))
	defer srvA.Close()
	srvB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		record(r, &hitsB)
		_, _ = w.Write([]byte("data: b\n\n"))
	}))
	defer srvB.Close()

	proxy := New(newFakeResolver([]string{srvA.URL, srvB.URL}, nil), "X-Tenant-ID", nil)

	for i := 0; i < 4; i++ {
		rec := httptest.NewRecorder()
		proxy.ServeHTTP(rec, newChatRequest("svc-a"))
		if rec.Code != http.StatusOK {
			t.Fatalf("第 %d 次请求应 200，实际 %d", i+1, rec.Code)
		}
	}
	if a, b := hitsA.Load(), hitsB.Load(); a != 2 || b != 2 {
		t.Fatalf("轮询应 2/2 均分，实际 A=%d B=%d", a, b)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, p := range gotPaths {
		if p != "/api/v1/inference/svc-a/chat" {
			t.Fatalf("实例收到的路径应保持入站路径，实际 %q", p)
		}
	}
}

// TestProxy_HopByHopHeadersFiltered 验证实例响应中的 hop-by-hop 头被过滤不转发给客户端。
func TestProxy_HopByHopHeadersFiltered(t *testing.T) {
	upstreamSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
	defer upstreamSrv.Close()

	proxy := New(newFakeResolver([]string{upstreamSrv.URL}, nil), "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	// hop-by-hop 头不应出现在客户端响应中。
	for _, h := range []string{"Transfer-Encoding", "Upgrade"} {
		if v := rec.Header().Get(h); v != "" {
			t.Errorf("hop-by-hop 头 %q 不应转发，实际存在 %q", h, v)
		}
	}
	// 正常头应被转发。
	if v := rec.Header().Get("X-Custom-Header"); v != "custom-value" {
		t.Fatalf("X-Custom-Header 应转发为 custom-value，实际 %q", v)
	}
}

// TestProxy_UpstreamError 验证实例不可达时返回 502 并上报失败。
func TestProxy_UpstreamError(t *testing.T) {
	endpoint := "http://127.0.0.1:1" // 不可达端口。
	res := newFakeResolver([]string{endpoint}, nil)
	proxy := New(res, "X-Tenant-ID", nil)
	// 缩短超时以加速测试。
	proxy.client.Timeout = 200 * time.Millisecond

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("实例不可达应返回 502，实际 %d", rec.Code)
	}
	if got := res.failureCount(endpoint); got != 1 {
		t.Fatalf("应上报 1 次失败，实际 %d", got)
	}
}

// TestProxy_ForwardsClientHeaders 验证客户端有效头被转发到实例（剔除 hop-by-hop），
// 且 {svc} 与租户 ID 正确传给 Resolver。
func TestProxy_ForwardsClientHeaders(t *testing.T) {
	var receivedAuth, receivedTenant string
	upstreamSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedAuth = r.Header.Get("Authorization")
		receivedTenant = r.Header.Get("X-Tenant-ID")
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: ok\n\n"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	defer upstreamSrv.Close()

	res := newFakeResolver([]string{upstreamSrv.URL}, nil)
	proxy := New(res, "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	req := newChatRequest("svc-a")
	req.Header.Set("Authorization", "Bearer my-token")
	proxy.ServeHTTP(rec, req)

	if receivedAuth != "Bearer my-token" {
		t.Fatalf("Authorization 头应转发，实际 %q", receivedAuth)
	}
	if receivedTenant != "tenant-a" {
		t.Fatalf("X-Tenant-ID 头应转发，实际 %q", receivedTenant)
	}
	if res.lastSvc != "svc-a" {
		t.Fatalf("Resolver 应收到 svc=svc-a，实际 %q", res.lastSvc)
	}
	if res.lastTenant != "tenant-a" {
		t.Fatalf("Resolver 应收到 tenant=tenant-a，实际 %q", res.lastTenant)
	}
}

// TestProxy_NoHealthyUpstream 验证无可用实例时返回 503 与 NO_HEALTHY_UPSTREAM 错误信封。
func TestProxy_NoHealthyUpstream(t *testing.T) {
	proxy := New(newFakeResolver(nil, nil), "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("无实例应 503，实际 %d", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{`"code":"NO_HEALTHY_UPSTREAM"`, `"error"`, `"message"`} {
		if !strings.Contains(body, want) {
			t.Fatalf("503 错误体应包含 %s，实际: %s", want, body)
		}
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("错误响应 Content-Type 应为 application/json，实际 %q", ct)
	}
}

// TestProxy_ResolveSentinelErrors 验证各类解析哨兵错误统一映射为 503 + NO_HEALTHY_UPSTREAM。
func TestProxy_ResolveSentinelErrors(t *testing.T) {
	sentinels := []error{
		upstream.ErrServiceNotFound,
		upstream.ErrNotReady,
		upstream.ErrNoEndpoints,
		upstream.ErrNoHealthyEndpoints,
	}
	for _, sentinel := range sentinels {
		proxy := New(newFakeResolver(nil, sentinel), "X-Tenant-ID", nil)
		rec := httptest.NewRecorder()
		proxy.ServeHTTP(rec, newChatRequest("svc-a"))
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("哨兵 %v 应映射 503，实际 %d", sentinel, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), `"code":"NO_HEALTHY_UPSTREAM"`) {
			t.Fatalf("哨兵 %v 错误体应含 NO_HEALTHY_UPSTREAM，实际: %s", sentinel, rec.Body.String())
		}
	}
}

// TestProxy_ResolveTransientError 验证解析的网络/控制平面错误映射为 502。
func TestProxy_ResolveTransientError(t *testing.T) {
	proxy := New(newFakeResolver(nil, errors.New("connection refused")), "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("解析网络错误应 502，实际 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"code":"UPSTREAM_RESOLVE_FAILED"`) {
		t.Fatalf("错误体应含 UPSTREAM_RESOLVE_FAILED，实际: %s", rec.Body.String())
	}
}

// TestProxy_MissingSvc 验证路径缺少 {svc} 值时返回 400。
func TestProxy_MissingSvc(t *testing.T) {
	proxy := New(newFakeResolver([]string{"http://127.0.0.1:1"}, nil), "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/inference//chat", nil)
	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺少 svc 应 400，实际 %d", rec.Code)
	}
}

// TestProxy_InvalidEndpoint 验证 Resolver 返回非法 endpoint 时计失败并返回 502。
func TestProxy_InvalidEndpoint(t *testing.T) {
	endpoint := "://bad-endpoint"
	res := newFakeResolver([]string{endpoint}, nil)
	proxy := New(res, "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("非法 endpoint 应 502，实际 %d", rec.Code)
	}
	if got := res.failureCount(endpoint); got != 1 {
		t.Fatalf("非法 endpoint 应上报失败，实际 %d 次", got)
	}
}

// TestProxy_Upstream5xxCountsFailure 验证实例返回 5xx 时计一次失败（响应仍透传）。
func TestProxy_Upstream5xxCountsFailure(t *testing.T) {
	upstreamSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("boom"))
	}))
	defer upstreamSrv.Close()

	res := newFakeResolver([]string{upstreamSrv.URL}, nil)
	proxy := New(res, "X-Tenant-ID", nil)

	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, newChatRequest("svc-a"))

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("5xx 应透传，实际 %d", rec.Code)
	}
	if got := res.failureCount(upstreamSrv.URL); got != 1 {
		t.Fatalf("5xx 应计 1 次失败，实际 %d", got)
	}
	if got := res.successCount(upstreamSrv.URL); got != 0 {
		t.Fatalf("5xx 不应上报成功，实际 %d", got)
	}
}

// TestBuildUpstreamURL 验证实例基址与入站路径的拼接逻辑。
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
			upstream: "http://instance:8000",
			reqPath:  "/api/v1/inference/svc-a/chat",
			want:     "http://instance:8000/api/v1/inference/svc-a/chat",
		},
		{
			name:     "带 query 透传",
			upstream: "http://instance:8000",
			reqPath:  "/api/v1/inference/svc-a/chat",
			reqQuery: "stream=true",
			want:     "http://instance:8000/api/v1/inference/svc-a/chat?stream=true",
		},
		{
			name:     "endpoint 带路径前缀",
			upstream: "http://instance:8000/v1",
			reqPath:  "/chat",
			want:     "http://instance:8000/v1/chat",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			u, _ := url.Parse(tc.upstream)
			in, _ := url.Parse(tc.reqPath)
			if tc.reqQuery != "" {
				in.RawQuery = tc.reqQuery
			}
			got := buildUpstreamURL(u, in)
			if got != tc.want {
				t.Fatalf("buildUpstreamURL = %q, want %q", got, tc.want)
			}
		})
	}
}
