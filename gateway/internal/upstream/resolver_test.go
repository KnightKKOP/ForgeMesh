package upstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// svcA 是测试用的不带命名空间的推理服务。
var svcA = ServiceRef{Name: "svc-a"}

// readyBody 构造控制平面 GET /inference-services/{name} 的响应体，每个 endpoint 使用
// openai-chat 协议。
func readyBody(phase string, endpoints ...string) string {
	eps := ""
	for _, ep := range endpoints {
		eps += fmt.Sprintf(`{"url":%q,"protocol":"openai-chat"},`, ep)
	}
	if len(eps) > 0 {
		eps = eps[:len(eps)-1]
	}
	return fmt.Sprintf(`{"apiVersion":"forgemesh/v1","kind":"InferenceService",
		"metadata":{"name":"svc-a"},
		"spec":{},"status":{"phase":%q,"endpoints":[%s]}}`, phase, eps)
}

// newControlPlane 启动模拟控制平面，返回服务器与请求计数器。
// handler 为 nil 时返回 Ready + 两个 endpoint 的标准响应。
func newControlPlane(t *testing.T, handler http.HandlerFunc) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var calls atomic.Int64
	if handler == nil {
		handler = func(w http.ResponseWriter, r *http.Request) {

			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000", "http://10.0.0.2:8000"))
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/inference-services/", func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		handler(w, r)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, &calls
}

// newTestResolver 构造带静默 logger 的 Resolver。
func newTestResolver(cpURL string, ttl, cooldown time.Duration) *Resolver {
	return NewResolver(cpURL, nil, ttl, cooldown, log.New(io.Discard, "", 0))
}

// TestResolve_Success 验证正常解析：返回 status.endpoints，请求携带
// 正确路径与 X-Tenant-Id 头。
func TestResolve_Success(t *testing.T) {
	var gotPath, gotTenant, gotMethod string
	srv, calls := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotTenant, gotMethod = r.URL.Path, r.Header.Get("X-Tenant-Id"), r.Method
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000", "http://10.0.0.2:8000"))
	})

	r := newTestResolver(srv.URL, time.Second, time.Second)
	eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("Resolve 应成功，实际错误: %v", err)
	}
	if len(eps) != 2 || eps[0].URL != "http://10.0.0.1:8000" || eps[1].URL != "http://10.0.0.2:8000" {
		t.Fatalf("endpoints 不符: %v", eps)
	}
	if gotPath != "/api/v1/inference-services/svc-a" {
		t.Fatalf("请求路径不符: %q", gotPath)
	}
	if gotTenant != "tenant-a" {
		t.Fatalf("X-Tenant-Id 应为 tenant-a，实际 %q", gotTenant)
	}
	if gotMethod != http.MethodGet {
		t.Fatalf("应使用 GET，实际 %q", gotMethod)
	}
	if calls.Load() != 1 {
		t.Fatalf("应恰好发起 1 次请求，实际 %d", calls.Load())
	}
}

// TestResolve_ForwardsCallerAuthorization 验证 WithAuthorization 注入的凭证随查询发给控制平面，
// 未注入时不发送 Authorization。
func TestResolve_ForwardsCallerAuthorization(t *testing.T) {
	var mu sync.Mutex
	var got []string
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		got = append(got, r.Header.Get("Authorization"))
		mu.Unlock()
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Nanosecond, time.Second)

	if _, err := r.Resolve(WithAuthorization(context.Background(), "Bearer fmk_k"), svcA, "tenant-a"); err != nil {
		t.Fatalf("Resolve 应成功: %v", err)
	}
	time.Sleep(time.Millisecond)
	if _, err := r.Resolve(WithAuthorization(context.Background(), ""), svcA, "tenant-a"); err != nil {
		t.Fatalf("Resolve 应成功: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(got) != 2 || got[0] != "Bearer fmk_k" || got[1] != "" {
		t.Fatalf("控制平面收到的 Authorization 应为 [Bearer fmk_k, 空]，实际 %q", got)
	}
}

// TestResolve_Forbidden 验证控制平面 403 映射为 ErrForbidden。
func TestResolve_Forbidden(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); !errors.Is(err, ErrForbidden) {
		t.Fatalf("403 应映射为 ErrForbidden，实际 %v", err)
	}
}

// TestResolve_CacheHit 验证 TTL 内第二次 Resolve 命中缓存，不再发请求。
func TestResolve_CacheHit(t *testing.T) {
	srv, calls := newControlPlane(t, nil)
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	for i := 0; i < 3; i++ {
		if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
			t.Fatalf("第 %d 次 Resolve 失败: %v", i+1, err)
		}
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("TTL 内应只发 1 次请求，实际 %d", got)
	}
}

// TestResolve_CacheExpiry 验证 TTL 过期后重新查询控制平面。
func TestResolve_CacheExpiry(t *testing.T) {
	srv, calls := newControlPlane(t, nil)
	r := newTestResolver(srv.URL, 50*time.Millisecond, time.Second)

	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("首次 Resolve 失败: %v", err)
	}
	time.Sleep(70 * time.Millisecond)
	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("过期后 Resolve 失败: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("TTL 过期后应重新请求（共 2 次），实际 %d", got)
	}
}

// TestResolve_TenantIsolation 验证缓存 key 含租户：不同租户各自查询。
func TestResolve_TenantIsolation(t *testing.T) {
	srv, calls := newControlPlane(t, nil)
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("tenant-a Resolve 失败: %v", err)
	}
	if _, err := r.Resolve(context.Background(), svcA, "tenant-b"); err != nil {
		t.Fatalf("tenant-b Resolve 失败: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("不同租户应各自发请求（共 2 次），实际 %d", got)
	}
}

// TestResolve_NotFound 验证控制平面 404 → ErrServiceNotFound。
func TestResolve_NotFound(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"detail":"InferenceService 'svc-a' not found"}`)
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if !errors.Is(err, ErrServiceNotFound) {
		t.Fatalf("应返回 ErrServiceNotFound，实际: %v", err)
	}
}

// TestResolve_ServerError 验证控制平面 5xx 返回普通错误（非哨兵）。
func TestResolve_ServerError(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err == nil {
		t.Fatal("控制平面 500 应返回错误")
	}
	for _, sentinel := range []error{ErrServiceNotFound, ErrNotReady, ErrNoEndpoints, ErrNoHealthyEndpoints} {
		if errors.Is(err, sentinel) {
			t.Fatalf("500 不应映射为哨兵 %v，实际: %v", sentinel, err)
		}
	}
}

// TestResolve_NetworkError 验证控制平面不可达时返回普通错误。
func TestResolve_NetworkError(t *testing.T) {
	r := newTestResolver("http://127.0.0.1:1", time.Second, time.Second)
	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err == nil {
		t.Fatal("网络不可达应返回错误")
	}
	if errors.Is(err, ErrServiceNotFound) {
		t.Fatalf("网络错误不应误判为 404: %v", err)
	}
}

// TestResolve_EmptyEndpoints 验证 status.endpoints 为空 → ErrNoEndpoints。
func TestResolve_EmptyEndpoints(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready"))
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if !errors.Is(err, ErrNoEndpoints) {
		t.Fatalf("应返回 ErrNoEndpoints，实际: %v", err)
	}
}

// TestResolve_MissingEndpointsField 验证旧版控制平面（无 endpoints 字段）
// 按"无实例"处理，而非崩溃或误判成功。
func TestResolve_MissingEndpointsField(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"apiVersion":"forgemesh/v1","kind":"InferenceService",
			"metadata":{"name":"svc-a"},"spec":{},"status":{"phase":"Ready"}}`)
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if !errors.Is(err, ErrNoEndpoints) {
		t.Fatalf("缺失 endpoints 字段应返回 ErrNoEndpoints，实际: %v", err)
	}
}

// TestResolve_InvalidEndpointsFiltered 验证非法 endpoint 被过滤。
func TestResolve_InvalidEndpointsFiltered(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready", "not-a-url", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("Resolve 应成功: %v", err)
	}
	if len(eps) != 1 || eps[0].URL != "http://10.0.0.1:8000" {
		t.Fatalf("应仅保留合法 endpoint，实际: %v", eps)
	}
}

// TestResolve_NotReady 验证 phase 非 Ready → ErrNotReady。
func TestResolve_NotReady(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Pending", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Second, time.Second)

	_, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if !errors.Is(err, ErrNotReady) {
		t.Fatalf("应返回 ErrNotReady，实际: %v", err)
	}
}

// TestResolve_FailureEjection 验证连续失败达到阈值后被摘流：
// 多实例场景摘除故障实例，单实例场景返回 ErrNoHealthyEndpoints。
func TestResolve_FailureEjection(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000", "http://10.0.0.2:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Hour) // 冷却足够长，避免测试中恢复

	// 前 2 次失败：仍有两个 endpoint。
	r.ReportFailure("http://10.0.0.1:8000")
	r.ReportFailure("http://10.0.0.1:8000")
	eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("未达阈值不应摘流: %v", err)
	}
	if len(eps) != 2 {
		t.Fatalf("未达阈值应保留 2 个 endpoint，实际 %v", eps)
	}

	// 第 3 次失败：达到阈值，实例 A 被摘流。
	r.ReportFailure("http://10.0.0.1:8000")
	eps, err = r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("摘流一个实例后仍应可解析: %v", err)
	}
	if len(eps) != 1 || eps[0].URL != "http://10.0.0.2:8000" {
		t.Fatalf("应只剩实例 B，实际: %v", eps)
	}

	// 实例 B 也被摘流：全部不可用。
	r.ReportFailure("http://10.0.0.2:8000")
	r.ReportFailure("http://10.0.0.2:8000")
	r.ReportFailure("http://10.0.0.2:8000")
	_, err = r.Resolve(context.Background(), svcA, "tenant-a")
	if !errors.Is(err, ErrNoHealthyEndpoints) {
		t.Fatalf("全部摘流应返回 ErrNoHealthyEndpoints，实际: %v", err)
	}
}

// TestResolve_CooldownRecovery 验证冷却期结束后实例重新可用。
func TestResolve_CooldownRecovery(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, 60*time.Millisecond)

	for i := 0; i < defaultFailureThreshold; i++ {
		r.ReportFailure("http://10.0.0.1:8000")
	}
	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); !errors.Is(err, ErrNoHealthyEndpoints) {
		t.Fatalf("摘流期应返回 ErrNoHealthyEndpoints，实际: %v", err)
	}

	time.Sleep(80 * time.Millisecond) // 等待冷却结束（half-open 放行）。
	eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("冷却结束后应恢复可用: %v", err)
	}
	if len(eps) != 1 || eps[0].URL != "http://10.0.0.1:8000" {
		t.Fatalf("恢复后应返回原 endpoint，实际: %v", eps)
	}
}

// TestReportSuccess_ResetsFailures 验证成功上报清零连续失败计数：
// 失败×2 → 成功 → 失败×2 不会被摘流（连续计数不累计）。
func TestReportSuccess_ResetsFailures(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Hour)

	r.ReportFailure("http://10.0.0.1:8000")
	r.ReportFailure("http://10.0.0.1:8000")
	r.ReportSuccess("http://10.0.0.1:8000")
	r.ReportFailure("http://10.0.0.1:8000")
	r.ReportFailure("http://10.0.0.1:8000")

	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("穿插成功后连续失败未达阈值，不应摘流: %v", err)
	}
}

// TestNewResolver_Defaults 验证非正参数回退默认值。
func TestNewResolver_Defaults(t *testing.T) {
	r := NewResolver("http://cp:8080", nil, 0, -1, nil)
	if r.ttl != DefaultResolverTTL {
		t.Fatalf("ttl 应回退默认 %v，实际 %v", DefaultResolverTTL, r.ttl)
	}
	if r.cooldown != DefaultUnhealthyCooldown {
		t.Fatalf("cooldown 应回退默认 %v，实际 %v", DefaultUnhealthyCooldown, r.cooldown)
	}
	if r.negativeTTL != negativeCacheTTL {
		t.Fatalf("负缓存时长应为 %v，实际 %v", negativeCacheTTL, r.negativeTTL)
	}
	if short := NewResolver("http://cp:8080", nil, 100*time.Millisecond, 0, nil); short.negativeTTL != 100*time.Millisecond {
		t.Fatalf("负缓存时长不应超过 ttl，实际 %v", short.negativeTTL)
	}
	if r.client == nil || r.client.Timeout != resolveHTTPTimeout {
		t.Fatalf("client 为 nil 时应构造默认超时客户端")
	}
}

// TestResolve_Concurrent 并发 Resolve + 成败上报，验证内部锁的正确性
// （配合 go test -race 检测数据竞争）。
func TestResolve_Concurrent(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000", "http://10.0.0.2:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Hour)

	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _ = r.Resolve(context.Background(), svcA, "tenant-a")
			r.ReportFailure(fmt.Sprintf("http://10.0.0.%d:8000", i%2+1))
			r.ReportSuccess(fmt.Sprintf("http://10.0.0.%d:8000", i%2+1))
		}(i)
	}
	wg.Wait()
}

// blockingControlPlane 启动在 release 调用之前挂起全部查询的模拟控制平面。
func blockingControlPlane(t *testing.T) (*httptest.Server, *atomic.Int64, func()) {
	t.Helper()
	gate := make(chan struct{})
	var once sync.Once
	release := func() { once.Do(func() { close(gate) }) }
	srv, calls := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		<-gate
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	// Cleanup 后进先出：先放行挂起的请求，srv.Close 才不会一直等待。
	t.Cleanup(release)
	return srv, calls, release
}

// waitForCalls 等待控制平面收到 n 次查询。
func waitForCalls(t *testing.T, calls *atomic.Int64, n int64) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for calls.Load() < n {
		if time.Now().After(deadline) {
			t.Fatalf("控制平面应收到 %d 次查询，实际 %d", n, calls.Load())
		}
		time.Sleep(time.Millisecond)
	}
}

// TestResolve_CoalescesConcurrentMisses 验证同一服务的并发未命中只查询一次控制平面，
// 全部调用方拿到这次查询的结果。
func TestResolve_CoalescesConcurrentMisses(t *testing.T) {
	srv, calls, release := blockingControlPlane(t)
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	const callers = 20
	var started sync.WaitGroup
	results := make(chan error, callers)
	for i := 0; i < callers; i++ {
		started.Add(1)
		go func() {
			started.Done()
			eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
			if err == nil && (len(eps) != 1 || eps[0].URL != "http://10.0.0.1:8000") {
				err = fmt.Errorf("endpoints 不符: %v", eps)
			}
			results <- err
		}()
	}
	started.Wait()
	waitForCalls(t, calls, 1)
	time.Sleep(20 * time.Millisecond) // 留出时间让其余调用方进入等待，否则测不出重复查询。
	release()

	for i := 0; i < callers; i++ {
		if err := <-results; err != nil {
			t.Fatalf("共用查询的调用方应成功: %v", err)
		}
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("并发未命中应只查询 1 次控制平面，实际 %d", got)
	}
}

// TestResolve_CallerCancellationDoesNotAbortSharedLookup 验证发起查询的调用方取消后查询继续：
// 该调用方返回 context.Canceled，等待同一查询的调用方仍拿到结果。
func TestResolve_CallerCancellationDoesNotAbortSharedLookup(t *testing.T) {
	srv, calls, release := blockingControlPlane(t)
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	first := make(chan error, 1)
	go func() {
		_, err := r.Resolve(ctx, svcA, "tenant-a")
		first <- err
	}()
	waitForCalls(t, calls, 1)

	second := make(chan error, 1)
	go func() {
		_, err := r.Resolve(context.Background(), svcA, "tenant-a")
		second <- err
	}()

	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatalf("取消的调用方应返回 context.Canceled，实际 %v", err)
	}
	release()
	if err := <-second; err != nil {
		t.Fatalf("等待同一查询的调用方应成功: %v", err)
	}
	if got := calls.Load(); got != 1 {
		t.Fatalf("取消不应中断共用的查询（应只查询 1 次），实际 %d", got)
	}
}

// TestResolve_CachesMisses 验证"服务不存在 / 未就绪 / 无可用实例"在负缓存期内不再查询控制平面。
func TestResolve_CachesMisses(t *testing.T) {
	for name, tc := range map[string]struct {
		status int
		body   string
		want   error
	}{
		"not found":    {http.StatusNotFound, `{"detail":"not found"}`, ErrServiceNotFound},
		"not ready":    {http.StatusOK, readyBody("Pending", "http://10.0.0.1:8000"), ErrNotReady},
		"no endpoints": {http.StatusOK, readyBody("Ready"), ErrNoEndpoints},
	} {
		t.Run(name, func(t *testing.T) {
			srv, calls := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(tc.status)
				_, _ = io.WriteString(w, tc.body)
			})
			r := newTestResolver(srv.URL, time.Minute, time.Second)

			for i := 0; i < 3; i++ {
				if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); !errors.Is(err, tc.want) {
					t.Fatalf("第 %d 次 Resolve 应返回 %v，实际 %v", i+1, tc.want, err)
				}
			}
			if got := calls.Load(); got != 1 {
				t.Fatalf("负缓存期内应只查询 1 次控制平面，实际 %d", got)
			}
		})
	}
}

// TestResolve_MissCacheExpires 验证否定结果按 negativeTTL（而不是 ttl）过期：服务就绪后
// 很快就能解析成功。
func TestResolve_MissCacheExpires(t *testing.T) {
	var ready atomic.Bool
	srv, calls := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		phase := "Pending"
		if ready.Load() {
			phase = "Ready"
		}
		_, _ = io.WriteString(w, readyBody(phase, "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Second)
	r.negativeTTL = 50 * time.Millisecond

	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); !errors.Is(err, ErrNotReady) {
		t.Fatalf("应返回 ErrNotReady，实际 %v", err)
	}
	ready.Store(true)
	time.Sleep(70 * time.Millisecond)
	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("负缓存过期后应重新查询并成功: %v", err)
	}
	if got := calls.Load(); got != 2 {
		t.Fatalf("应查询 2 次控制平面，实际 %d", got)
	}
}

// TestResolve_DoesNotCacheOtherFailures 验证 403 / 401 / 5xx 不缓存：它们取决于凭证或属于
// 暂时故障，下一个请求立即重新查询。
func TestResolve_DoesNotCacheOtherFailures(t *testing.T) {
	for name, status := range map[string]int{
		"forbidden":    http.StatusForbidden,
		"unauthorized": http.StatusUnauthorized,
		"server error": http.StatusInternalServerError,
	} {
		t.Run(name, func(t *testing.T) {
			srv, calls := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			})
			r := newTestResolver(srv.URL, time.Minute, time.Second)

			for i := 0; i < 2; i++ {
				if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err == nil {
					t.Fatalf("控制平面返回 %d 时 Resolve 应失败", status)
				}
			}
			if got := calls.Load(); got != 2 {
				t.Fatalf("失败不应缓存（应查询 2 次），实际 %d", got)
			}
		})
	}
}

// TestResolve_QueriesTheServiceNamespace 验证带命名空间的服务以 namespace 参数查询，
// 不带命名空间的服务不发送该参数（与控制平面的语义一致）。
func TestResolve_QueriesTheServiceNamespace(t *testing.T) {
	var mu sync.Mutex
	var queries []string
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		queries = append(queries, r.URL.Path+"?"+r.URL.RawQuery)
		mu.Unlock()
		_, _ = io.WriteString(w, readyBody("Ready", "http://10.0.0.1:8000"))
	})
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	if _, err := r.Resolve(context.Background(), ServiceRef{Namespace: "team a", Name: "svc-a"}, "tenant-a"); err != nil {
		t.Fatalf("带命名空间的 Resolve 失败: %v", err)
	}
	if _, err := r.Resolve(context.Background(), svcA, "tenant-a"); err != nil {
		t.Fatalf("不带命名空间的 Resolve 失败: %v", err)
	}

	want := []string{
		"/api/v1/inference-services/svc-a?namespace=team+a",
		"/api/v1/inference-services/svc-a?",
	}
	if fmt.Sprint(queries) != fmt.Sprint(want) {
		t.Fatalf("查询不符（命名空间也应区分缓存）:\n got %v\nwant %v", queries, want)
	}
}

// TestResolve_KeepsOnlyReadyEndpointsWithKnownProtocols 验证未就绪、协议未知或缺失协议、
// 非 http(s) 的实例被跳过，保留的实例带上协议。
func TestResolve_KeepsOnlyReadyEndpointsWithKnownProtocols(t *testing.T) {
	srv, _ := newControlPlane(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.WriteString(w, `{"kind":"InferenceService","status":{"phase":"Ready","endpoints":[
			{"url":"http://10.0.0.1:8000","protocol":"openai-chat","ready":false},
			{"url":"http://10.0.0.2:8000","protocol":"grpc"},
			{"url":"http://10.0.0.3:8000"},
			{"url":"ftp://10.0.0.4","protocol":"tgi-generate"},
			{"url":"http://10.0.0.5:8080","protocol":"tgi-generate","ready":true},
			{"url":"http://10.0.0.6:8000","protocol":"openai-chat"}]}}`)
	})
	r := newTestResolver(srv.URL, time.Minute, time.Second)

	eps, err := r.Resolve(context.Background(), svcA, "tenant-a")
	if err != nil {
		t.Fatalf("Resolve 应成功: %v", err)
	}
	want := []Endpoint{
		{URL: "http://10.0.0.5:8080", Protocol: ProtocolTGIGenerate},
		{URL: "http://10.0.0.6:8000", Protocol: ProtocolOpenAIChat},
	}
	if fmt.Sprint(eps) != fmt.Sprint(want) {
		t.Fatalf("endpoints 不符:\n got %v\nwant %v", eps, want)
	}
}

// TestProtocolPath 验证协议到实例推理路径的映射。
func TestProtocolPath(t *testing.T) {
	for protocol, want := range map[Protocol]string{
		ProtocolOpenAIChat:  "/v1/chat/completions",
		ProtocolTGIGenerate: "/generate_stream",
	} {
		if got, ok := protocol.Path(); !ok || got != want {
			t.Fatalf("%s 应映射到 %s，实际 %q (ok=%v)", protocol, want, got, ok)
		}
	}
	if _, ok := Protocol("grpc").Path(); ok {
		t.Fatal("未知协议不应有路径")
	}
}
