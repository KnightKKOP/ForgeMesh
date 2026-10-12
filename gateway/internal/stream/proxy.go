// Package stream 实现 SSE / 流式分块的透明代理。
//
// 与普通反代的区别：
//   - 设置 text/event-stream 并禁用各级缓冲（含 nginx 的 X-Accel-Buffering）。
//   - 逐块读取上游响应并立即 Flush，保证 token 实时到达客户端。
//   - 通过继承客户端请求的 context，客户端断开会自动取消上游请求，避免资源泄漏。
//
// 路由语义：入站路径 /api/v1/inference/{svc}/chat 中的 {svc} 通过 Resolver
// 解析为推理服务实例 endpoint，按简单轮询选择一个实例转发（实例级负载均衡），
// 并依据转发成败向 Resolver 上报，驱动健康摘流与冷却恢复。
//
// 安全约束（见开发计划 §14.1）：仅做转发，不缓存、不落盘任何响应内容。
package stream

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/upstream"
)

// readBufferSize 是单次读取上游响应体的缓冲区大小。
const readBufferSize = 32 * 1024

// Resolver 抽象「推理服务名 → 实例 endpoint 列表」的解析与健康上报，
// 由 upstream.Resolver 实现；以接口注入便于测试替换。
type Resolver interface {
	// Resolve 返回服务的健康实例 endpoint 列表（如 "http://10.0.0.1:8000"）。
	Resolve(ctx context.Context, svc, tenantID string) ([]string, error)
	// ReportFailure 上报一次实例转发失败（驱动摘流）。
	ReportFailure(endpoint string)
	// ReportSuccess 上报一次实例转发成功（清零失败计数）。
	ReportSuccess(endpoint string)
}

// Proxy 是流式代理处理器，实现 http.Handler。
type Proxy struct {
	resolver     Resolver
	tenantHeader string
	client       *http.Client
	logger       *log.Logger
	rr           atomic.Uint64 // 轮询计数器
}

// New 构造流式代理。
//
// resolver 提供按服务名的实例解析；tenantHeader 为提取租户身份的请求头名
// （与认证中间件一致）；logger 为 nil 时使用 log.Default()。
//
// 客户端不设置整体超时（流式连接可能长时间保持），仅对响应头到达设置超时，
// 防止上游僵死连接长期占用资源。
func New(resolver Resolver, tenantHeader string, logger *log.Logger) *Proxy {
	if logger == nil {
		logger = log.Default()
	}
	if resolver == nil {
		panic("stream: New 需要非空 resolver")
	}
	if tenantHeader == "" {
		tenantHeader = "X-Tenant-ID"
	}
	return &Proxy{
		resolver:     resolver,
		tenantHeader: tenantHeader,
		logger:       logger,
		client: &http.Client{
			Transport: &http.Transport{
				ResponseHeaderTimeout: 30 * time.Second,
				DialContext: (&net.Dialer{
					Timeout:   10 * time.Second,
					KeepAlive: 30 * time.Second,
				}).DialContext,
				// 关闭响应压缩，避免代理层引入额外缓冲/转换。
				DisableCompression: true,
			},
		},
	}
}

// ServeHTTP 解析 {svc} → 选择实例 → 转发单个流式请求。
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	svc := r.PathValue("svc")
	if svc == "" {
		writeJSONError(w, http.StatusBadRequest, "MISSING_SERVICE", "路径缺少推理服务名 {svc}")
		return
	}
	tenantID := r.Header.Get(p.tenantHeader)

	resolveCtx := upstream.WithAuthorization(r.Context(), r.Header.Get("Authorization"))
	endpoints, err := p.resolver.Resolve(resolveCtx, svc, tenantID)
	if err != nil {
		p.logger.Printf("stream resolve error: corr=%s svc=%s tenant=%s err=%v",
			r.Header.Get("X-Correlation-ID"), svc, tenantID, err)
		status, code := classifyResolveError(err)
		writeJSONError(w, status, code, err.Error())
		return
	}
	if len(endpoints) == 0 {
		writeJSONError(w, http.StatusServiceUnavailable, "NO_HEALTHY_UPSTREAM",
			"推理服务 "+svc+" 当前没有可用实例")
		return
	}

	// 简单轮询：原子递增取模，保证多实例间均匀分发。
	endpoint := endpoints[int(p.rr.Add(1))%len(endpoints)]
	target, err := url.Parse(endpoint)
	if err != nil || target.Scheme == "" || target.Host == "" {
		p.logger.Printf("stream invalid endpoint: corr=%s svc=%s endpoint=%q err=%v",
			r.Header.Get("X-Correlation-ID"), svc, endpoint, err)
		p.resolver.ReportFailure(endpoint)
		writeJSONError(w, http.StatusBadGateway, "INVALID_UPSTREAM",
			"实例地址非法: "+endpoint)
		return
	}

	p.forward(w, r, target, endpoint)
}

// forward 执行单次流式转发，并依据结果向 Resolver 上报健康度。
func (p *Proxy) forward(w http.ResponseWriter, r *http.Request, target *url.URL, endpoint string) {
	// 使用客户端请求的 context：客户端断开时，Go 运行时会取消该 context，
	// 进而取消这里的上游请求，实现“断连即取消上游”。
	ctx := r.Context()

	req, err := http.NewRequestWithContext(ctx, r.Method, buildUpstreamURL(target, r.URL), r.Body)
	if err != nil {
		http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
		return
	}
	req.Header = copyForwardHeaders(r.Header)
	// 显式沿用入站内容长度，交由 Transport 正确编码请求体。
	req.ContentLength = r.ContentLength
	// 流式场景强制接受 SSE。
	req.Header.Set("Accept", "text/event-stream")

	resp, err := p.client.Do(req)
	if err != nil {
		p.logger.Printf("stream upstream error: corr=%s path=%s endpoint=%s err=%v",
			r.Header.Get("X-Correlation-ID"), r.URL.Path, endpoint, err)
		p.resolver.ReportFailure(endpoint)
		http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// 实例返回 5xx 视为实例侧故障（计入摘流），但响应仍透传给客户端；
	// 4xx 属客户端问题，不计故障。
	upstreamFailed := resp.StatusCode >= 500
	if upstreamFailed {
		p.resolver.ReportFailure(endpoint)

	}

	// 透传上游头，但剔除 hop-by-hop 头，避免连接语义污染。
	dst := w.Header()
	for k, vs := range resp.Header {
		if isHopByHop(k) {
			continue
		}
		dst[k] = append([]string(nil), vs...)
	}
	// 强制流式语义。
	dst.Set("Content-Type", "text/event-stream")
	dst.Set("Cache-Control", "no-cache")
	dst.Set("Connection", "keep-alive")
	// 关闭 nginx 等中间层对响应的缓冲。
	dst.Set("X-Accel-Buffering", "no")

	w.WriteHeader(resp.StatusCode)

	flusher, _ := w.(http.Flusher)
	if flusher != nil {
		flusher.Flush() // 尽早把响应头推给客户端，建立流通道
	}

	buf := make([]byte, readBufferSize)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, writeErr := w.Write(buf[:n]); writeErr != nil {
				// 客户端已断开：直接返回，defer 会关闭上游响应体并取消上游请求。
				// 断连属客户端行为，不计入实例健康度。
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			// 客户端断开导致上游请求被取消：属客户端行为，不计实例健康度。
			if errors.Is(readErr, context.Canceled) || errors.Is(readErr, context.DeadlineExceeded) {
				return
			}
			if readErr != io.EOF {
				// 上游流中途异常终止：计一次实例失败。
				p.logger.Printf("stream read error: corr=%s path=%s endpoint=%s err=%v",
					r.Header.Get("X-Correlation-ID"), r.URL.Path, endpoint, readErr)
				p.resolver.ReportFailure(endpoint)
				return
			}
			// 流完整读尽：实例工作正常（5xx 响应已单独计失败，不覆盖）。
			if !upstreamFailed {
				p.resolver.ReportSuccess(endpoint)
			}
			return
		}
		// 客户端主动取消时立即停止读取上游。
		if ctx.Err() != nil {
			return
		}
	}
}

// classifyResolveError 将解析错误映射为 (HTTP 状态码, 错误码)。
// 凭证无权读取服务 → 403；服务缺失/未就绪/无实例 → 503；控制平面自身故障 → 502。
func classifyResolveError(err error) (int, string) {
	switch {
	case errors.Is(err, upstream.ErrForbidden):
		return http.StatusForbidden, "FORBIDDEN"
	case errors.Is(err, upstream.ErrServiceNotFound),
		errors.Is(err, upstream.ErrNotReady),
		errors.Is(err, upstream.ErrNoEndpoints),
		errors.Is(err, upstream.ErrNoHealthyEndpoints):
		return http.StatusServiceUnavailable, "NO_HEALTHY_UPSTREAM"
	default:
		return http.StatusBadGateway, "UPSTREAM_RESOLVE_FAILED"
	}
}

// writeJSONError 输出与控制平面错误信封风格对齐的 JSON 错误体：
//
//	{"error":{"code":"...","message":"..."}}
func writeJSONError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	body := struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}{}
	body.Error.Code = code
	body.Error.Message = message
	// 序列化失败只可能源于内部构造，忽略并保留已写出的状态码。
	_ = json.NewEncoder(w).Encode(body)
}

// buildUpstreamURL 拼接实例基路径与入站请求路径，避免出现双斜杠。
func buildUpstreamURL(base *url.URL, in *url.URL) string {
	u := *base
	u.Path = singleJoiningSlash(base.Path, in.Path)
	u.RawQuery = in.RawQuery
	return u.String()
}

// singleJoiningSlash 拼接两段路径，保证连接处恰好一个斜杠。
func singleJoiningSlash(a, b string) string {
	aslash := strings.HasSuffix(a, "/")
	bslash := strings.HasPrefix(b, "/")
	switch {
	case aslash && bslash:
		return a + b[1:]
	case !aslash && !bslash:
		if a == "" {
			return b
		}
		return a + "/" + b
	}
	return a + b
}

// hopByHopHeaders 是逐跳头（小写），不应被代理转发。
var hopByHopHeaders = map[string]struct{}{
	"connection":          {},
	"proxy-connection":    {},
	"keep-alive":          {},
	"proxy-authenticate":  {},
	"proxy-authorization": {},
	"te":                  {},
	"trailer":             {},
	"transfer-encoding":   {},
	"upgrade":             {},
}

// isHopByHop 判断请求/响应头是否为逐跳头（大小写不敏感）。
func isHopByHop(key string) bool {
	_, ok := hopByHopHeaders[strings.ToLower(key)]
	return ok
}

// copyForwardHeaders 复制需要转发的入站头，剔除逐跳头、Host 与 Content-Length
// （后者由 Transport 依据 ContentLength 字段自动生成，避免长度不一致）。
func copyForwardHeaders(src http.Header) http.Header {
	dst := make(http.Header, len(src))
	for k, vs := range src {
		ck := http.CanonicalHeaderKey(k)
		if isHopByHop(k) || ck == "Host" || ck == "Content-Length" {
			continue
		}
		dst[k] = append([]string(nil), vs...)
	}
	return dst
}
