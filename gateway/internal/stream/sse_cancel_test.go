// sse_cancel_test.go 验证 SSE 流式代理的取消与断连语义（WP-D1）：
//
//   - 客户端取消（request context cancel）后，代理级联取消上游请求，
//     上游连接在超时界限内被释放（不依赖长 sleep，用 channel 断言）；
//   - 上游异常断开时客户端在超时界限内得到明确结果（非挂死）；
//     上游连接失败时客户端收到 502 明确错误；
//   - 流式响应逐块转发：SSE data: 行按上游 Flush 节奏逐块到达客户端，
//     代理层不引入整流缓冲。
//
// 测试采用真实 HTTP 客户端 + httptest 网关（ServeMux 提供 {svc} 路径变量），
// 以覆盖"断连即取消上游"的完整传播链；复用 proxy_test.go 中的 fakeResolver。
package stream

import (
	"bufio"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// newGatewayServer 将 proxy 挂载到与线上一致的路由模式，返回完整网关 server。
func newGatewayServer(t *testing.T, res Resolver) *httptest.Server {
	t.Helper()
	proxy := New(res, "X-Tenant-ID", allowLoopback, nil)
	mux := http.NewServeMux()
	mux.Handle("/api/v1/inference/{svc}/chat", proxy)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

// awaitCh 在 timeout 内等待 channel 信号，超时则使测试失败。
func awaitCh(t *testing.T, ch <-chan struct{}, what string, timeout time.Duration) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(timeout):
		t.Fatalf("等待 %s 超时（>%v）", what, timeout)
	}
}

// TestSSE_ClientCancelReleasesUpstream 验证客户端取消级联释放上游：
// 客户端读到一个 SSE 块后取消请求 → 网关入站 ctx 取消 → 代理取消上游请求
// → 上游 handler 的 request context 在 3s 内被取消（连接释放）。
func TestSSE_ClientCancelReleasesUpstream(t *testing.T) {
	chunkSent := make(chan struct{}, 1) // 上游已发出首个块
	released := make(chan struct{})     // 上游观测到客户端断开（ctx 取消）
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: first\n\n"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		chunkSent <- struct{}{}
		<-r.Context().Done() // 阻塞直到网关取消上游请求
		close(released)
	}))
	t.Cleanup(upstream.Close)

	gw := newGatewayServer(t, newFakeResolver([]string{upstream.URL}, nil))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, gw.URL+"/api/v1/inference/svc-a/chat", nil)
	if err != nil {
		t.Fatalf("构造请求失败: %v", err)
	}
	req.Header.Set("X-Tenant-ID", "tenant-a")

	type result struct {
		resp *http.Response
		err  error
	}
	got := make(chan result, 1)
	go func() {
		resp, err := http.DefaultClient.Do(req)
		got <- result{resp, err}
	}()

	var res result
	select {
	case res = <-got:
	case <-time.After(3 * time.Second):
		t.Fatal("等待响应头超时")
	}
	if res.err != nil {
		t.Fatalf("请求失败: %v", res.err)
	}
	defer res.resp.Body.Close()

	// 读取首个 SSE 块，确认流建立后取消。
	awaitCh(t, chunkSent, "上游发出首个块", 3*time.Second)
	line, err := bufio.NewReader(res.resp.Body).ReadString('\n')
	if err != nil {
		t.Fatalf("读取首个 SSE 行失败: %v", err)
	}
	if strings.TrimSpace(line) != "data: first" {
		t.Fatalf("首个 SSE 行应为 data: first，实际 %q", line)
	}
	cancel()

	// 上游应在取消后立即被释放（ctx 级联取消），3s 界限内断言。
	awaitCh(t, released, "客户端取消后上游连接释放", 3*time.Second)
}

// TestSSE_UpstreamAbruptDisconnectClientNotHang 验证上游流中途异常断开
// （hijack 后直接关闭连接，产生非 EOF 终止）时：客户端在超时界限内收到
// 已转发的数据并结束读取（不挂死），且代理向 Resolver 上报该实例失败。
func TestSSE_UpstreamAbruptDisconnectClientNotHang(t *testing.T) {
	firstChunk := "data: partial\n\n"
	clientGotFirst := make(chan struct{}) // 客户端确认已收到首块
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(firstChunk))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		// 等客户端确认收到首块后再粗暴断开，保证断开发生在流中途。
		<-clientGotFirst
		hj, ok := w.(http.Hijacker)
		if !ok {
			t.Error("httptest server 应支持 Hijack")
			return
		}
		conn, _, err := hj.Hijack()
		if err != nil {
			t.Errorf("hijack 失败: %v", err)
			return
		}
		conn.Close() // 模拟实例异常断连（连接重置，非正常 EOF）
	}))
	t.Cleanup(upstream.Close)

	res := newFakeResolver([]string{upstream.URL}, nil)
	gw := newGatewayServer(t, res)

	client := &http.Client{Timeout: 5 * time.Second} // 整体上限：若挂死则超时报错
	resp, err := client.Post(gw.URL+"/api/v1/inference/svc-a/chat", "text/event-stream", nil)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()

	// 读取首块并确认，驱动上游断开。
	line, err := bufio.NewReader(resp.Body).ReadString('\n')
	if err != nil {
		t.Fatalf("读取首块失败: %v", err)
	}
	if strings.TrimSpace(line) != "data: partial" {
		t.Fatalf("首块应为 data: partial，实际 %q", line)
	}
	close(clientGotFirst)

	// 剩余读取应在断连后快速结束（不挂死），3s 界限断言。
	rest := make(chan string, 1)
	go func() {
		b, _ := io.ReadAll(resp.Body)
		rest <- string(b)
	}()
	select {
	case <-rest:
	case <-time.After(3 * time.Second):
		t.Fatal("上游断开后客户端读取应快速结束，实际挂死 >3s")
	}
	if got := res.failureCount(upstream.URL); got < 1 {
		t.Fatalf("上游异常断开应上报失败，实际 %d 次", got)
	}
}

// TestSSE_UpstreamConnectErrorClientGets502 验证上游连接失败（实例不可达）时，
// 客户端在超时界限内收到明确的 502 错误而非挂死。
func TestSSE_UpstreamConnectErrorClientGets502(t *testing.T) {
	gw := newGatewayServer(t, newFakeResolver([]string{"http://127.0.0.1:1"}, nil))

	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Post(gw.URL+"/api/v1/inference/svc-a/chat", "text/event-stream", nil)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("上游连接失败应 502，实际 %d", resp.StatusCode)
	}
}

// TestSSE_ChunksForwardedIncrementally 验证逐块转发（无整流缓冲）：
// 上游每发出一个 SSE 块后等待客户端确认（ack）才发下一块；
// 客户端必须依次收到 chunk-1/2/3 —— 若代理层缓冲整流，客户端读不到
// chunk-1，上游将因 ack 超时中断，本测试随即失败。
func TestSSE_ChunksForwardedIncrementally(t *testing.T) {
	const totalChunks = 3
	acks := make([]chan struct{}, totalChunks)
	for i := range acks {
		acks[i] = make(chan struct{})
	}

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		for i := 0; i < totalChunks; i++ {
			_, _ = w.Write([]byte("data: chunk-" + string(rune('1'+i)) + "\n\n"))
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
			select {
			case <-acks[i]: // 客户端已收到第 i+1 块，才发送下一块
			case <-time.After(3 * time.Second):
				return // ack 超时：客户端未收到本块（疑似被缓冲整流）
			}
		}
	}))
	t.Cleanup(upstream.Close)

	gw := newGatewayServer(t, newFakeResolver([]string{upstream.URL}, nil))

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Post(gw.URL+"/api/v1/inference/svc-a/chat", "text/event-stream", nil)
	if err != nil {
		t.Fatalf("请求失败: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("SSE 请求应 200，实际 %d", resp.StatusCode)
	}
	if ct := resp.Header.Get("Content-Type"); ct != "text/event-stream" {
		t.Fatalf("Content-Type 应为 text/event-stream，实际 %q", ct)
	}

	// 客户端逐行读取：每读到第 i 块的 data 行即回 ack，驱动上游发下一块。
	reader := bufio.NewReader(resp.Body)
	for i := 0; i < totalChunks; i++ {
		var dataLine string
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				t.Fatalf("读取第 %d 块失败: %v（可能被代理层缓冲整流导致上游 ack 超时）", i+1, err)
			}
			trimmed := strings.TrimRight(line, "\n")
			if strings.HasPrefix(trimmed, "data: ") {
				dataLine = trimmed
				break
			}
			// 跳过空行/注释行等其他 SSE 语义行。
		}
		want := "data: chunk-" + string(rune('1'+i))
		if dataLine != want {
			t.Fatalf("第 %d 块应为 %q，实际 %q（顺序或完整性错误）", i+1, want, dataLine)
		}
		close(acks[i]) // 已收到第 i+1 块，允许上游继续
	}
}
