// Package stream 实现 SSE / 流式分块的透明代理。
//
// 与普通反代的区别：
//   - 设置 text/event-stream 并禁用各级缓冲（含 nginx 的 X-Accel-Buffering）。
//   - 逐块读取上游响应并立即 Flush，保证 token 实时到达客户端。
//   - 通过继承客户端请求的 context，客户端断开会自动取消上游请求，避免资源泄漏。
//
// 安全约束（见开发计划 §14.1）：仅做转发，不缓存、不落盘任何响应内容。
package stream

import (
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// readBufferSize 是单次读取上游响应体的缓冲区大小。
const readBufferSize = 32 * 1024

// Proxy 是流式代理处理器，实现 http.Handler。
type Proxy struct {
	upstream *url.URL
	client   *http.Client
	logger   *log.Logger
}

// New 构造流式代理。
//
// 客户端不设置整体超时（流式连接可能长时间保持），仅对响应头到达设置超时，
// 防止上游僵死连接长期占用资源。
func New(target *url.URL, logger *log.Logger) *Proxy {
	if logger == nil {
		logger = log.Default()
	}
	if target == nil {
		panic("stream: New 需要非空 target")
	}
	return &Proxy{
		upstream: target,
		logger:   logger,
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

// ServeHTTP 转发单个流式请求。
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// 使用客户端请求的 context：客户端断开时，Go 运行时会取消该 context，
	// 进而取消这里的上游请求，实现“断连即取消上游”。
	ctx := r.Context()

	req, err := http.NewRequestWithContext(ctx, r.Method, p.buildUpstreamURL(r.URL), r.Body)
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
		p.logger.Printf("stream upstream error: corr=%s path=%s err=%v", r.Header.Get("X-Correlation-ID"), r.URL.Path, err)
		http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

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
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			if readErr != io.EOF {
				p.logger.Printf("stream read error: corr=%s path=%s err=%v", r.Header.Get("X-Correlation-ID"), r.URL.Path, readErr)
			}
			return
		}
		// 客户端主动取消时立即停止读取上游。
		if ctx.Err() != nil {
			return
		}
	}
}

// buildUpstreamURL 拼接上游基路径与入站请求路径，避免出现双斜杠。
func (p *Proxy) buildUpstreamURL(in *url.URL) string {
	u := *p.upstream
	u.Path = singleJoiningSlash(p.upstream.Path, in.Path)
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