// resolver_test.go 从网关入口端到端验证推理路径的 Resolver 行为（WP-D1）：
//
//   - TTL 缓存：endpoints 更新后 TTL 内仍用旧值，过期后重新拉取；
//   - 健康摘流：同一 endpoint 连续 3 次失败后进入冷却期，不再被轮询选中；
//   - 轮询：多实例依次轮转；
//   - 错误响应映射：服务不存在 / 未就绪 / 无实例 / 全部冷却 → 503，
//     控制平面自身故障 → 502，实例不可达 → 502。
//
// 时间控制说明：Router.Config 的 TTL/cooldown 为秒级整数（最小 1s），无法
// 注入时钟；因此 TTL 用例采用 1s TTL + 短等待（~1.3s）的边界断言，
// 摘流用例用长冷却（600s）避免测试期内 half-open 恢复，避免长 sleep。
package router

import (
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
)

// fakeInstance 是记录命中数的模拟推理实例；status 控制响应状态码。
type fakeInstance struct {
	url    string
	status int
	hits   atomic.Int64
}

func newFakeInstance(t *testing.T, status int) *fakeInstance {
	t.Helper()
	ins := &fakeInstance{status: status}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ins.hits.Add(1)
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(status)
		_, _ = w.Write([]byte("data: tick\n\n"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
	}))
	t.Cleanup(srv.Close)
	ins.url = srv.URL
	return ins
}

// resolverEnv 组装假控制平面 + 网关；endpoints 可动态切换，并统计
// 控制平面被查询次数（仅 /api/v1/inference-services/ 前缀）。
type resolverEnv struct {
	router *Router
	mu     sync.Mutex
	eps    []string
	cpHits atomic.Int64
}

func newResolverEnv(t *testing.T, ttlSec, cooldownSec int, eps ...string) *resolverEnv {
	t.Helper()
	env := &resolverEnv{eps: eps}
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/api/v1/inference-services/") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		env.cpHits.Add(1)
		env.mu.Lock()
		eps := append([]string(nil), env.eps...)
		env.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, readyEndpointsJSON("Ready", eps))
	}))
	t.Cleanup(cp.Close)

	cfg := config.Config{
		ListenAddr:               ":0",
		UpstreamURL:              cp.URL,
		RateLimitRPS:             10000,
		RateLimitBurst:           10000,
		TenantHeader:             "X-Tenant-ID",
		CorrelationHeader:        "X-Correlation-ID",
		ResolverTTLSeconds:       ttlSec,
		UnhealthyCooldownSeconds: cooldownSec,
		AuthMode:                 config.AuthModeDev,
	}
	r, err := New(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	env.router = r
	return env
}

func (e *resolverEnv) setEndpoints(eps ...string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.eps = eps
}

// doChat 经网关发起一次推理请求，返回 recorder。
func (e *resolverEnv) doChat(t *testing.T, svc string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/"+svc+"/chat", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	e.router.ServeHTTP(rec, req)
	return rec
}

// readyEndpointsJSON 构造控制平面 status.endpoints 响应体。
func readyEndpointsJSON(phase string, eps []string) string {
	quoted := make([]string, 0, len(eps))
	for _, ep := range eps {
		quoted = append(quoted, fmt.Sprintf("%q", ep))
	}
	return fmt.Sprintf(`{"apiVersion":"forgemesh/v1","kind":"InferenceService",`+
		`"metadata":{"name":"svc-a"},"spec":{},`+
		`"status":{"phase":%q,"endpoints":[%s]}}`, phase, strings.Join(quoted, ","))
}

// TestResolver_TTLCacheServesStaleUntilExpiry 验证 TTL 缓存语义：
// TTL（1s）内 endpoints 更新后仍用旧值（缓存命中、控制平面不被重复查询），
// 过期后下一次请求重新拉取并切换到新值。
func TestResolver_TTLCacheServesStaleUntilExpiry(t *testing.T) {
	oldIns := newFakeInstance(t, http.StatusOK)
	newIns := newFakeInstance(t, http.StatusOK)
	env := newResolverEnv(t, 1, 600, oldIns.url)

	// 第一次请求：填充缓存，命中旧实例。
	if rec := env.doChat(t, "svc-a"); rec.Code != http.StatusOK {
		t.Fatalf("首次请求应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	if got := env.cpHits.Load(); got != 1 {
		t.Fatalf("首次请求后控制平面应被查询 1 次，实际 %d", got)
	}

	// 控制平面 endpoints 更新为新实例；TTL 内立即请求 → 仍命中旧实例。
	env.setEndpoints(newIns.url)
	start := time.Now()
	rec := env.doChat(t, "svc-a")
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatalf("TTL 内请求耗时 %v 已超过 TTL，用例无效（需保证在缓存期内发出）", elapsed)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("TTL 内请求应 200，实际 %d", rec.Code)
	}
	if got := env.cpHits.Load(); got != 1 {
		t.Fatalf("TTL 内应命中缓存不重新查询，控制平面查询 %d 次", got)
	}
	if got := oldIns.hits.Load(); got != 2 {
		t.Fatalf("TTL 内应继续命中旧实例（累计 2 次），实际 %d", got)
	}
	if got := newIns.hits.Load(); got != 0 {
		t.Fatalf("TTL 内新实例不应被命中，实际 %d", got)
	}

	// 等待 TTL 过期后请求 → 刷新为新实例。
	time.Sleep(1300 * time.Millisecond)
	if rec := env.doChat(t, "svc-a"); rec.Code != http.StatusOK {
		t.Fatalf("过期后请求应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	if got := env.cpHits.Load(); got != 2 {
		t.Fatalf("过期后应重新查询控制平面（共 2 次），实际 %d", got)
	}
	if got := newIns.hits.Load(); got != 1 {
		t.Fatalf("过期后应切换到新实例，实际命中 %d 次", got)
	}
}

// TestResolver_RoundRobinAlternatesInstances 验证多实例轮询：
// 6 次请求在两个实例间依次轮转、均匀分配。
func TestResolver_RoundRobinAlternatesInstances(t *testing.T) {
	a := newFakeInstance(t, http.StatusOK)
	b := newFakeInstance(t, http.StatusOK)
	env := newResolverEnv(t, 60, 600, a.url, b.url)

	for i := 0; i < 6; i++ {
		if rec := env.doChat(t, "svc-a"); rec.Code != http.StatusOK {
			t.Fatalf("第 %d 次请求应 200，实际 %d", i+1, rec.Code)
		}
	}
	if got := a.hits.Load(); got != 3 {
		t.Fatalf("实例 A 应命中 3 次，实际 %d", got)
	}
	if got := b.hits.Load(); got != 3 {
		t.Fatalf("实例 B 应命中 3 次，实际 %d", got)
	}
}

// TestResolver_ConsecutiveFailuresEjectEndpoint 验证健康摘流：
// 实例 A 持续 5xx，连续 3 次失败后进入长冷却期，不再被轮询选中；
// 此后全部流量落在健康实例 B 上。
func TestResolver_ConsecutiveFailuresEjectEndpoint(t *testing.T) {
	a := newFakeInstance(t, http.StatusInternalServerError) // 恒定 5xx
	b := newFakeInstance(t, http.StatusOK)
	env := newResolverEnv(t, 60, 600, a.url, b.url) // 冷却 600s，测试期内不恢复

	// 轮询序列（A/B 交替）：B,A,B,A,B,A —— 第 3 次命中 A 时达到摘流阈值。
	for i := 0; i < 6; i++ {
		rec := env.doChat(t, "svc-a")
		// A 的 5xx 透传给客户端（500），B 正常 200；两种都算完成一次分发。
		if rec.Code != http.StatusOK && rec.Code != http.StatusInternalServerError {
			t.Fatalf("第 %d 次请求意外状态 %d（body=%s）", i+1, rec.Code, rec.Body.String())
		}
	}
	if got := a.hits.Load(); got != 3 {
		t.Fatalf("实例 A 应恰在第 3 次命中后达到摘流阈值，实际命中 %d 次", got)
	}

	// 摘流生效：后续请求全部落在 B，A 不再被选中。
	for i := 0; i < 4; i++ {
		if rec := env.doChat(t, "svc-a"); rec.Code != http.StatusOK {
			t.Fatalf("摘流后第 %d 次请求应 200，实际 %d", i+1, rec.Code)
		}
	}
	if got := a.hits.Load(); got != 3 {
		t.Fatalf("冷却期内实例 A 不应再被选中，实际命中 %d 次", got)
	}
	if got := b.hits.Load(); got != 7 { // 前 6 次中的 3 次 + 后 4 次
		t.Fatalf("实例 B 应承接全部剩余流量（7 次），实际 %d", got)
	}
}

// TestResolver_AllEndpointsEjectedReturns503 验证唯一实例被摘流后
// （缓存内 endpoint 全部处于冷却期），请求返回 503 与 NO_HEALTHY_UPSTREAM 信封。
func TestResolver_AllEndpointsEjectedReturns503(t *testing.T) {
	a := newFakeInstance(t, http.StatusInternalServerError)
	env := newResolverEnv(t, 60, 600, a.url)

	// 3 次失败 → 唯一实例进入冷却。
	for i := 0; i < 3; i++ {
		if rec := env.doChat(t, "svc-a"); rec.Code != http.StatusInternalServerError {
			t.Fatalf("第 %d 次请求应透传实例 500，实际 %d", i+1, rec.Code)
		}
	}

	rec := env.doChat(t, "svc-a")
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("全部实例冷却应 503，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"code":"NO_HEALTHY_UPSTREAM"`) {
		t.Fatalf("错误体应含 NO_HEALTHY_UPSTREAM 信封，实际: %s", body)
	}
	if got := a.hits.Load(); got != 3 {
		t.Fatalf("冷却期内实例不应再被选中，实际命中 %d 次", got)
	}
}

// TestResolver_ErrorResponseMapping 表驱动验证控制平面异常的响应映射：
// 404 / 未就绪 / 无实例 → 503 NO_HEALTHY_UPSTREAM；
// 控制平面 5xx / 非法响应体 → 502 UPSTREAM_RESOLVE_FAILED。
func TestResolver_ErrorResponseMapping(t *testing.T) {
	cases := []struct {
		name     string
		phase    string
		eps      []string
		status   int
		body     string
		wantCode int
		wantJSON string
	}{
		{
			name: "服务不存在(404)", status: http.StatusNotFound,
			body: `{"detail":"not found"}`, wantCode: 503, wantJSON: `"code":"NO_HEALTHY_UPSTREAM"`,
		},
		{
			name: "服务未就绪(Pending)", phase: "Pending", eps: []string{"http://10.0.0.1:8000"},
			wantCode: 503, wantJSON: `"code":"NO_HEALTHY_UPSTREAM"`,
		},
		{
			name: "服务无实例(endpoints为空)", phase: "Ready",
			wantCode: 503, wantJSON: `"code":"NO_HEALTHY_UPSTREAM"`,
		},
		{
			name: "控制平面5xx", status: http.StatusInternalServerError,
			wantCode: 502, wantJSON: `"code":"UPSTREAM_RESOLVE_FAILED"`,
		},
		{
			name: "控制平面响应体非法", status: http.StatusOK, body: `not-json`,
			wantCode: 502, wantJSON: `"code":"UPSTREAM_RESOLVE_FAILED"`,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var hits atomic.Int64
			cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				hits.Add(1)
				status := tc.status
				if status == 0 {
					status = http.StatusOK
				}
				body := tc.body
				if body == "" {
					body = readyEndpointsJSON(tc.phase, tc.eps)
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(status)
				_, _ = io.WriteString(w, body)
			}))
			t.Cleanup(cp.Close)

			cfg := config.Config{
				ListenAddr:               ":0",
				UpstreamURL:              cp.URL,
				RateLimitRPS:             10000,
				RateLimitBurst:           10000,
				TenantHeader:             "X-Tenant-ID",
				CorrelationHeader:        "X-Correlation-ID",
				ResolverTTLSeconds:       60,
				UnhealthyCooldownSeconds: 600,
				AuthMode:                 config.AuthModeDev,
			}
			r, err := New(cfg, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatalf("New 失败: %v", err)
			}

			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
			req.Header.Set("X-Tenant-ID", "tenant-a")
			r.ServeHTTP(rec, req)

			if rec.Code != tc.wantCode {
				t.Fatalf("应映射 %d，实际 %d（body=%s）", tc.wantCode, rec.Code, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), tc.wantJSON) {
				t.Fatalf("错误体应含 %s，实际: %s", tc.wantJSON, rec.Body.String())
			}
			if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("错误响应 Content-Type 应为 application/json，实际 %q", ct)
			}
		})
	}
}

// TestResolver_UpstreamUnreachableMaps502 验证实例连接失败（不可达 endpoint）
// 映射为明确的 502 错误，客户端不会挂死。
// 注：Resolver 查询控制平面的 HTTP 超时固定为 5s（resolveHTTPTimeout），
// 单测不等待真实超时，以同类“连接失败”路径（立即拒绝）验证 502 映射。
func TestResolver_UpstreamUnreachableMaps502(t *testing.T) {
	env := newResolverEnv(t, 60, 600, "http://127.0.0.1:1") // 不可达端口

	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- env.doChat(t, "svc-a") }()

	select {
	case rec := <-done:
		if rec.Code != http.StatusBadGateway {
			t.Fatalf("实例不可达应 502，实际 %d（body=%s）", rec.Code, rec.Body.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("实例不可达应在超时内返回 502，而不是挂死")
	}
}
