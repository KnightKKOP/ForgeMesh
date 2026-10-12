// Package health 提供网关的存活/就绪探针。
//
//   - /healthz (liveness)：进程可服务即 200，不依赖外部组件。
//   - /readyz  (readiness)：请求控制平面的 /readyz，控制平面返回 200 才算就绪，
//     否则返回 503，以便负载均衡器将本实例摘除（配合优雅停机）。控制平面的
//     /readyz 会探测权威存储，进程在线但存储不可用时同样返回 503。
package health

import (
	"io"
	"net/http"
	"net/url"
	"time"
)

// probeTimeout 是就绪探针请求控制平面的超时时间。
const probeTimeout = 2 * time.Second

// maxProbeBodySize 限制读取并丢弃的控制平面响应体大小；读完响应体连接才能复用。
const maxProbeBodySize = 64 << 10

// Checker 提供健康检查处理器。
type Checker struct {
	readyURL string // 控制平面 /readyz 的地址；上游地址非法时为空
	client   *http.Client
}

// New 基于上游（控制平面）地址构造健康检查器。upstreamURL 非法时，就绪探针将始终返回 503。
func New(upstreamURL string) *Checker {
	return &Checker{
		readyURL: readyURL(upstreamURL),
		client:   &http.Client{Timeout: probeTimeout},
	}
}

// readyURL 返回控制平面 /readyz 的地址，保留上游地址的路径前缀（与反向代理拼接路径的
// 方式一致）；upstreamURL 不是 http(s) 绝对地址时返回空串。
func readyURL(upstreamURL string) string {
	u, err := url.Parse(upstreamURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return ""
	}
	return u.JoinPath("readyz").String()
}

// Live 处理存活探针，始终返回 200。
func (c *Checker) Live(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, `{"status":"ok"}`)
}

// Ready 处理就绪探针：请求控制平面的 /readyz，只有 200 视为就绪。
func (c *Checker) Ready(w http.ResponseWriter, r *http.Request) {
	if c.readyURL == "" {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"invalid upstream"}`)
		return
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, c.readyURL, nil)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"invalid upstream"}`)
		return
	}
	resp, err := c.client.Do(req)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"upstream unreachable"}`)
		return
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxProbeBodySize))
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"upstream not ready"}`)
		return
	}

	writeJSON(w, http.StatusOK, `{"status":"ready"}`)
}

func writeJSON(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}
