package upstream

import (
	"net/http"
	"testing"
)

// TestNewControlPlaneTransport 验证控制平面 Transport 沿用默认设置（含环境变量代理），
// 放宽每个主机的空闲连接数，并限制等待响应头的时间。
func TestNewControlPlaneTransport(t *testing.T) {
	transport := newControlPlaneTransport()
	defaults := http.DefaultTransport.(*http.Transport)

	if transport.MaxIdleConnsPerHost <= http.DefaultMaxIdleConnsPerHost {
		t.Fatalf("每个主机的空闲连接数应高于默认的 %d，实际 %d",
			http.DefaultMaxIdleConnsPerHost, transport.MaxIdleConnsPerHost)
	}
	if transport.MaxIdleConns < transport.MaxIdleConnsPerHost {
		t.Fatalf("MaxIdleConns（%d）不应小于 MaxIdleConnsPerHost（%d）",
			transport.MaxIdleConns, transport.MaxIdleConnsPerHost)
	}
	if transport.ResponseHeaderTimeout != controlPlaneResponseHeaderTimeout {
		t.Fatalf("ResponseHeaderTimeout 应为 %v，实际 %v",
			controlPlaneResponseHeaderTimeout, transport.ResponseHeaderTimeout)
	}
	if transport.Proxy == nil || transport.IdleConnTimeout != defaults.IdleConnTimeout ||
		transport.TLSHandshakeTimeout != defaults.TLSHandshakeTimeout {
		t.Fatal("其余设置应沿用 http.DefaultTransport")
	}
	if transport == defaults {
		t.Fatal("不应修改共享的 http.DefaultTransport")
	}
}

// TestNewResolver_DefaultClientUsesControlPlaneTransport 验证 Resolver 的默认客户端使用
// 放宽了连接池的 Transport，而不是共享的 http.DefaultTransport。
func TestNewResolver_DefaultClientUsesControlPlaneTransport(t *testing.T) {
	r := NewResolver("http://cp:8080", nil, 0, 0, nil)
	transport, ok := r.client.Transport.(*http.Transport)
	if !ok || transport == http.DefaultTransport {
		t.Fatalf("默认客户端应使用独立的 *http.Transport，实际 %T", r.client.Transport)
	}
	if transport.MaxIdleConnsPerHost <= http.DefaultMaxIdleConnsPerHost {
		t.Fatalf("每个主机的空闲连接数应高于默认值，实际 %d", transport.MaxIdleConnsPerHost)
	}
}
