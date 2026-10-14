package upstream

import (
	"context"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
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

func lookupsKey(result metrics.LookupResult) string {
	return `forgemesh_gateway_resolver_lookups_total{result="` + string(result) + `"}`
}

// controlPlaneErrors 返回各类控制平面错误的计数（没有出现的类别不在结果中）。
func controlPlaneErrors(t *testing.T, g *metrics.Gateway) map[string]float64 {
	t.Helper()
	const prefix = `forgemesh_gateway_upstream_errors_total{upstream="control_plane",kind="`
	errs := make(map[string]float64)
	for key, value := range scrape(t, g) {
		if kind, ok := strings.CutPrefix(key, prefix); ok {
			errs[strings.TrimSuffix(kind, `"}`)] = value
		}
	}
	return errs
}

// TestResolverMetrics_CountsHitsAndMisses 验证首次解析计 miss、缓存期内计 hit，
// 缓存的否定结果（服务不存在）同样计 hit。
func TestResolverMetrics_CountsHitsAndMisses(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/missing") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Minute)
	g := metrics.NewGateway()
	r.SetMetrics(g)

	for _, svc := range []ServiceRef{svcA, svcA, {Name: "missing"}, {Name: "missing"}, svcA} {
		_, _ = r.Resolve(context.Background(), svc, "tenant-a")
	}

	samples := scrape(t, g)
	want := map[metrics.LookupResult]float64{
		metrics.LookupMiss: 2, metrics.LookupHit: 3, metrics.LookupCoalesced: 0,
	}
	for result, value := range want {
		if got := samples[lookupsKey(result)]; got != value {
			t.Errorf("%s = %v，want %v", result, got, value)
		}
	}
}

// TestResolverMetrics_CountsCoalescedLookups 验证并入进行中查询的调用方计 coalesced，
// 不重复计 miss。
func TestResolverMetrics_CountsCoalescedLookups(t *testing.T) {
	srv, calls, release := blockingControlPlane(t)
	r := newTestResolver(srv.URL, time.Minute, time.Minute)
	g := metrics.NewGateway()
	r.SetMetrics(g)

	results := make(chan error, 2)
	resolve := func() {
		_, err := r.Resolve(context.Background(), svcA, "tenant-a")
		results <- err
	}
	go resolve()
	waitForCalls(t, calls, 1)
	go resolve()
	deadline := time.Now().Add(2 * time.Second)
	for scrape(t, g)[lookupsKey(metrics.LookupCoalesced)] != 1 {
		if time.Now().After(deadline) {
			t.Fatal("第二个调用方应并入进行中的查询")
		}
		time.Sleep(time.Millisecond)
	}
	release()
	for i := 0; i < 2; i++ {
		if err := <-results; err != nil {
			t.Fatalf("调用方应成功: %v", err)
		}
	}

	samples := scrape(t, g)
	if miss, hit := samples[lookupsKey(metrics.LookupMiss)], samples[lookupsKey(metrics.LookupHit)]; miss != 1 || hit != 0 {
		t.Fatalf("应计 1 次 miss、0 次 hit，实际 miss=%v hit=%v", miss, hit)
	}
}

// hijackAfter 返回一个先写出 head 再直接关闭连接的处理器（响应体在中途中断）。
func hijackAfter(t *testing.T, head string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		conn, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Errorf("hijack 失败: %v", err)
			return
		}
		_, _ = io.WriteString(conn, head)
		conn.Close()
	}
}

// TestResolverMetrics_CountsControlPlaneErrors 验证 Resolver 查询控制平面的错误按类别计数：
// 连接失败 connect、5xx status、响应体读取或解析失败 body；404 / 403 / 其他 4xx 与未就绪
// 是正常的否定结果，不计。
func TestResolverMetrics_CountsControlPlaneErrors(t *testing.T) {
	cases := map[string]struct {
		handler http.HandlerFunc
		want    map[string]float64
	}{
		"5xx": {
			handler: func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusServiceUnavailable) },
			want:    map[string]float64{"status": 1},
		},
		"非法 JSON": {
			handler: func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, "{") },
			want:    map[string]float64{"body": 1},
		},
		"响应体中断": {
			handler: hijackAfter(t, "HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"),
			want:    map[string]float64{"body": 1},
		},
		"404": {
			handler: func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNotFound) },
			want:    map[string]float64{},
		},
		"403": {
			handler: func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusForbidden) },
			want:    map[string]float64{},
		},
		"422": {
			handler: func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusUnprocessableEntity) },
			want:    map[string]float64{},
		},
		"未就绪": {
			handler: func(w http.ResponseWriter, _ *http.Request) { _, _ = io.WriteString(w, readyBody("Pending")) },
			want:    map[string]float64{},
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			srv, _ := newControlPlane(t, tc.handler)
			r := newTestResolver(srv.URL, time.Minute, time.Minute)
			g := metrics.NewGateway()
			r.SetMetrics(g)
			if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err == nil {
				t.Fatal("解析应失败")
			}
			assertErrors(t, controlPlaneErrors(t, g), tc.want)
		})
	}

	t.Run("连接失败", func(t *testing.T) {
		r := newTestResolver("http://127.0.0.1:1", time.Minute, time.Minute)
		g := metrics.NewGateway()
		r.SetMetrics(g)
		if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err == nil {
			t.Fatal("解析应失败")
		}
		assertErrors(t, controlPlaneErrors(t, g), map[string]float64{"connect": 1})
	})
}

func assertErrors(t *testing.T, got, want map[string]float64) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("错误计数应为 %v，实际 %v", want, got)
	}
	for kind, value := range want {
		if got[kind] != value {
			t.Fatalf("错误计数应为 %v，实际 %v", want, got)
		}
	}
}

// TestResolver_InstanceStates 验证实例状态只统计未过期的缓存条目中的不同实例 URL，
// 冷却期内的实例计 unhealthy。
func TestResolver_InstanceStates(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/svc-b") {
			_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.2:8000", "http://10.0.0.3:8000"))
			return
		}
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000", "http://10.0.0.2:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Minute)
	states := func() [2]int {
		healthy, unhealthy := r.InstanceStates()
		return [2]int{healthy, unhealthy}
	}
	if got := states(); got != [2]int{0, 0} {
		t.Fatalf("缓存为空时应为 0/0，实际 %v", got)
	}

	svcB := ServiceRef{Name: "svc-b"}
	for _, svc := range []ServiceRef{svcA, svcB} {
		if _, err := r.Resolve(context.Background(), svc, "tenant-a"); err != nil {
			t.Fatalf("解析 %s 失败: %v", svc, err)
		}
	}
	if got := states(); got != [2]int{3, 0} {
		t.Fatalf("两个服务共用的实例只计一次，应为 3/0，实际 %v", got)
	}

	for i := 0; i < defaultFailureThreshold; i++ {
		r.ReportFailure("http://10.0.0.2:8000")
	}
	if got := states(); got != [2]int{2, 1} {
		t.Fatalf("摘流的实例应计 unhealthy，应为 2/1，实际 %v", got)
	}

	r.mu.Lock()
	entry := r.cache[cacheKey("tenant-a", svcB)]
	entry.expiry = time.Now().Add(-time.Second)
	r.cache[cacheKey("tenant-a", svcB)] = entry
	r.mu.Unlock()
	if got := states(); got != [2]int{1, 1} {
		t.Fatalf("过期的缓存条目不应计入，应为 1/1，实际 %v", got)
	}
}

// TestClientMetrics_CountsControlPlaneErrors 验证资源接口反代把控制平面 5xx 计 status、
// 连接失败计 connect；4xx 与客户端已断开的请求不计。
func TestClientMetrics_CountsControlPlaneErrors(t *testing.T) {
	status := http.StatusBadGateway
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
	}))
	defer cp.Close()
	newClient := func(rawURL string) (*Client, *metrics.Gateway) {
		target, err := url.Parse(rawURL)
		if err != nil {
			t.Fatal(err)
		}
		c := New(target, "X-Correlation-ID", "X-Tenant-ID", log.New(io.Discard, "", 0))
		g := metrics.NewGateway()
		c.SetMetrics(g)
		return c, g
	}
	serve := func(c *Client, ctx context.Context) int {
		rec := httptest.NewRecorder()
		c.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil).WithContext(ctx))
		return rec.Code
	}

	c, g := newClient(cp.URL)
	serve(c, context.Background())
	status = http.StatusNotFound
	serve(c, context.Background())
	assertErrors(t, controlPlaneErrors(t, g), map[string]float64{"status": 1})

	c, g = newClient("http://127.0.0.1:1")
	if code := serve(c, context.Background()); code != http.StatusBadGateway {
		t.Fatalf("控制平面不可达应 502，实际 %d", code)
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	serve(c, canceled)
	assertErrors(t, controlPlaneErrors(t, g), map[string]float64{"connect": 1})
}
