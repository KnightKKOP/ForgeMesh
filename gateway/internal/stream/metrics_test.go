package stream

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics/metricstest"
)

// scrape 抓取 g 的输出并按文本格式严格解析。
func scrape(t *testing.T, g *metrics.Gateway) map[string]float64 {
	t.Helper()
	rec := httptest.NewRecorder()
	g.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return metricstest.MustParse(t, rec.Body.String())
}

// instanceErrors 返回各类实例错误的计数（没有出现的类别不在结果中）。
func instanceErrors(t *testing.T, g *metrics.Gateway) map[string]float64 {
	t.Helper()
	const prefix = `forgemesh_gateway_upstream_errors_total{upstream="instance",kind="`
	errs := make(map[string]float64)
	for key, value := range scrape(t, g) {
		if kind, ok := strings.CutPrefix(key, prefix); ok {
			errs[strings.TrimSuffix(kind, `"}`)] = value
		}
	}
	return errs
}

func newMeteredProxy(res Resolver, policy AddressPolicy) (*Proxy, *metrics.Gateway) {
	p := New(res, "X-Tenant-ID", policy, log.New(io.Discard, "", 0))
	g := metrics.NewGateway()
	p.SetMetrics(g)
	return p, g
}

// TestProxyMetrics_ObservesFirstByteOfSuccessfulStreams 验证首字节时间计到实例第一个响应体
// 字节写给客户端（不是响应头到达），且只统计 2xx 的流。
func TestProxyMetrics_ObservesFirstByteOfSuccessfulStreams(t *testing.T) {
	const delay = 30 * time.Millisecond
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		time.Sleep(delay)
		_, _ = io.WriteString(w, "data: first\n\n")
	}))
	defer srv.Close()

	p, g := newMeteredProxy(newFakeResolver([]string{srv.URL}, nil), allowLoopback)
	p.ServeHTTP(httptest.NewRecorder(), newChatRequest("svc-a"))

	samples := scrape(t, g)
	count := samples["forgemesh_gateway_stream_first_byte_seconds_count"]
	sum := samples["forgemesh_gateway_stream_first_byte_seconds_sum"]
	if count != 1 || sum < delay.Seconds() {
		t.Fatalf("应记录 1 次不短于 %v 的首字节时间，实际 count=%v sum=%v", delay, count, sum)
	}

	for _, status := range []int{http.StatusBadRequest, http.StatusInternalServerError} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
			_, _ = io.WriteString(w, `{"error":"bad"}`)
		}))
		p, g := newMeteredProxy(newFakeResolver([]string{srv.URL}, nil), allowLoopback)
		p.ServeHTTP(httptest.NewRecorder(), newChatRequest("svc-a"))
		srv.Close()
		if got := scrape(t, g)["forgemesh_gateway_stream_first_byte_seconds_count"]; got != 0 {
			t.Fatalf("实例返回 %d 时不应记录首字节时间，实际 %v 次", status, got)
		}
	}
}

// TestProxyMetrics_CountsInstanceErrors 验证实例错误按类别计数；4xx 与客户端已断开的请求不计。
func TestProxyMetrics_CountsInstanceErrors(t *testing.T) {
	respond := func(status int) string {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(status)
			_, _ = io.WriteString(w, "data: x\n\n")
		}))
		t.Cleanup(srv.Close)
		return srv.URL
	}
	broken := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: partial\n\n")
		w.(http.Flusher).Flush()
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Errorf("hijack 失败: %v", err)
			return
		}
		conn.Close()
	}))
	t.Cleanup(broken.Close)

	cases := map[string]struct {
		endpoint string
		policy   AddressPolicy
		canceled bool
		want     map[string]float64
	}{
		"连接失败":   {endpoint: "http://127.0.0.1:1", policy: allowLoopback, want: map[string]float64{"connect": 1}},
		"地址不允许":  {endpoint: respond(http.StatusOK), want: map[string]float64{"address_not_allowed": 1}},
		"地址非法":   {endpoint: "://bad-endpoint", policy: allowLoopback, want: map[string]float64{"invalid_endpoint": 1}},
		"5xx":    {endpoint: respond(http.StatusServiceUnavailable), policy: allowLoopback, want: map[string]float64{"status": 1}},
		"流中途中断":  {endpoint: broken.URL, policy: allowLoopback, want: map[string]float64{"body": 1}},
		"4xx":    {endpoint: respond(http.StatusBadRequest), policy: allowLoopback, want: map[string]float64{}},
		"客户端已断开": {endpoint: respond(http.StatusOK), policy: allowLoopback, canceled: true, want: map[string]float64{}},
		"正常":     {endpoint: respond(http.StatusOK), policy: allowLoopback, want: map[string]float64{}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			p, g := newMeteredProxy(newFakeResolver([]string{tc.endpoint}, nil), tc.policy)
			req := newChatRequest("svc-a")
			if tc.canceled {
				ctx, cancel := context.WithCancel(req.Context())
				cancel()
				req = req.WithContext(ctx)
			}
			p.ServeHTTP(httptest.NewRecorder(), req)

			got := instanceErrors(t, g)
			if len(got) != len(tc.want) {
				t.Fatalf("错误计数应为 %v，实际 %v", tc.want, got)
			}
			for kind, value := range tc.want {
				if got[kind] != value {
					t.Fatalf("错误计数应为 %v，实际 %v", tc.want, got)
				}
			}
		})
	}
}
