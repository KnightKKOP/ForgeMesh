// Package health 提供网关的存活/就绪探针。
//
//   - /healthz (liveness)：进程可服务即 200，不依赖外部组件。
//   - /readyz  (readiness)：探测上游可达性；不可达时返回 503，
//     以便负载均衡器将本实例摘除（配合优雅停机）。
package health

import (
	"net"
	"net/http"
	"net/url"
	"time"
)

// dialTimeout 是就绪探针连接上游的超时时间。
const dialTimeout = 2 * time.Second

// Checker 提供健康检查处理器。
type Checker struct {
	upstream *url.URL
	timeout  time.Duration
	// dial 可注入，便于单元测试替换 TCP 探测行为。
	dial func(network, address string, timeout time.Duration) (net.Conn, error)
}

// New 基于上游地址构造健康检查器。upstreamURL 非法时，就绪探针将始终返回 503。
func New(upstreamURL string) *Checker {
	u, _ := url.Parse(upstreamURL)
	return &Checker{
		upstream: u,
		timeout:  dialTimeout,
		dial:     net.DialTimeout,
	}
}

// Live 处理存活探针，始终返回 200。
func (c *Checker) Live(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, `{"status":"ok"}`)
}

// Ready 处理就绪探针：TCP 探测上游可达性。
func (c *Checker) Ready(w http.ResponseWriter, r *http.Request) {
	target := c.upstreamHostPort()
	if target == "" {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"invalid upstream"}`)
		return
	}

	dial := c.dial
	if dial == nil {
		dial = net.DialTimeout
	}
	conn, err := dial("tcp", target, c.timeout)
	if err != nil {
		writeJSON(w, http.StatusServiceUnavailable, `{"status":"unavailable","reason":"upstream unreachable"}`)
		return
	}
	_ = conn.Close()

	writeJSON(w, http.StatusOK, `{"status":"ready"}`)
}

// upstreamHostPort 返回上游的 host:port；缺省端口时按 scheme 补全。
func (c *Checker) upstreamHostPort() string {
	if c.upstream == nil || c.upstream.Host == "" {
		return ""
	}
	if c.upstream.Port() != "" {
		return c.upstream.Host
	}
	host := c.upstream.Hostname()
	switch c.upstream.Scheme {
	case "https":
		return net.JoinHostPort(host, "443")
	default:
		return net.JoinHostPort(host, "80")
	}
}

func writeJSON(w http.ResponseWriter, status int, body string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}