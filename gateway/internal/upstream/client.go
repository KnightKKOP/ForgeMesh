// Package upstream 封装指向 Python 控制平面的反向代理。
//
// 采用标准库 net/http/httputil.ReverseProxy，负责把 /api/v1/* 的资源类请求
// 透传到上游，并显式透传链路关联 ID 与租户身份头。
package upstream

import (
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
)

// Client 是对 httputil.ReverseProxy 的薄封装，实现 http.Handler。
type Client struct {
	proxy             *httputil.ReverseProxy
	correlationHeader string
	tenantHeader      string
}

// New 构造反向代理客户端。
//
// target 为上游基地址；correlationHeader / tenantHeader 为需要显式透传的头部名称；
// logger 为 nil 时使用 log.Default()。
func New(target *url.URL, correlationHeader, tenantHeader string, logger *log.Logger) *Client {
	if logger == nil {
		logger = log.Default()
	}
	if target == nil {
		panic("upstream: New 需要非空 target")
	}

	c := &Client{
		correlationHeader: correlationHeader,
		tenantHeader:      tenantHeader,
	}

	c.proxy = &httputil.ReverseProxy{
		// Rewrite 是 Go 1.20+ 推荐的请求改写入口（与 Director 互斥）。
		Rewrite: func(pr *httputil.ProxyRequest) {
			// 将请求路由到上游并保留原始路径 /api/v1/*。
			pr.SetURL(target)
			// 设置标准 X-Forwarded-* 头。
			pr.SetXForwarded()

			// 显式透传关联 ID 与租户头，确保下游可观测、可二次校验。
			// （ReverseProxy 默认会复制大多数头，此处显式设置以表达意图并防止被中间件丢弃。）
			if c.correlationHeader != "" {
				if cid := pr.In.Header.Get(c.correlationHeader); cid != "" {
					pr.Out.Header.Set(c.correlationHeader, cid)
				}
			}
			if c.tenantHeader != "" {
				if tenant := pr.In.Header.Get(c.tenantHeader); tenant != "" {
					pr.Out.Header.Set(c.tenantHeader, tenant)
				}
			}
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			logger.Printf("upstream proxy error: corr=%s path=%s err=%v", r.Header.Get(c.correlationHeader), r.URL.Path, err)
			http.Error(w, http.StatusText(http.StatusBadGateway), http.StatusBadGateway)
		},
	}

	return c
}

// ServeHTTP 实现 http.Handler，将请求反代到上游。
func (c *Client) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	c.proxy.ServeHTTP(w, r)
}
