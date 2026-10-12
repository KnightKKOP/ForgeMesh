package stream

import (
	"errors"
	"fmt"
	"net/netip"
	"syscall"
)

// ErrAddressNotAllowed 表示推理实例的地址不在允许连接的范围内。
var ErrAddressNotAllowed = errors.New("instance address is not allowed")

// metadataServices 是链路本地网段之外的云厂商元数据服务地址。
var metadataServices = []netip.Prefix{
	netip.MustParsePrefix("100.100.100.200/32"), // 阿里云
	netip.MustParsePrefix("fd00:ec2::254/128"),  // AWS（IPv6）
}

// AddressPolicy 决定流式代理可以连接哪些实例地址。
//
// 配置了允许网段时只允许其中的地址；零值（未配置）允许除回环、链路本地（含
// 169.254.169.254 等元数据服务）、未指定地址、组播与 metadataServices 之外的全部地址。
// 校验发生在建立连接时、针对解析出的 IP，主机名在解析之后改指他处也无法绕过。
type AddressPolicy struct {
	allowed []netip.Prefix
}

// NewAddressPolicy 返回只允许 allowed 网段的策略；allowed 为空时等同零值。
func NewAddressPolicy(allowed []netip.Prefix) AddressPolicy {
	return AddressPolicy{allowed: append([]netip.Prefix(nil), allowed...)}
}

// Allows 报告策略是否允许连接 addr。
func (p AddressPolicy) Allows(addr netip.Addr) bool {
	addr = addr.Unmap()
	if len(p.allowed) > 0 {
		for _, prefix := range p.allowed {
			if prefix.Contains(addr) {
				return true
			}
		}
		return false
	}
	if !addr.IsValid() || addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsUnspecified() ||
		addr.IsMulticast() {
		return false
	}
	for _, prefix := range metadataServices {
		if prefix.Contains(addr) {
			return false
		}
	}
	return true
}

// control 用作 net.Dialer.Control：在连接建立之前校验目标地址。
func (p AddressPolicy) control(_, address string, _ syscall.RawConn) error {
	target, err := netip.ParseAddrPort(address)
	if err != nil {
		return fmt.Errorf("%w: %s", ErrAddressNotAllowed, address)
	}
	if !p.Allows(target.Addr()) {
		return fmt.Errorf("%w: %s", ErrAddressNotAllowed, target.Addr())
	}
	return nil
}
