package stream

import (
	"errors"
	"net/netip"
	"testing"
)

// TestAddressPolicy_Default 验证未配置允许网段时拒绝回环、链路本地、元数据服务等地址。
func TestAddressPolicy_Default(t *testing.T) {
	cases := []struct {
		addr string
		want bool
	}{
		{"10.0.0.5", true},
		{"192.168.1.20", true},
		{"203.0.113.7", true},
		{"2001:db8::10", true},
		{"127.0.0.1", false},
		{"127.10.0.1", false},
		{"::1", false},
		{"::ffff:127.0.0.1", false},
		{"169.254.169.254", false},
		{"::ffff:169.254.169.254", false},
		{"169.254.0.23", false},
		{"fe80::1", false},
		{"100.100.100.200", false},
		{"fd00:ec2::254", false},
		{"0.0.0.0", false},
		{"::", false},
		{"224.0.0.1", false},
		{"ff02::1", false},
	}
	var policy AddressPolicy
	for _, tc := range cases {
		if got := policy.Allows(netip.MustParseAddr(tc.addr)); got != tc.want {
			t.Errorf("默认策略 Allows(%s) = %v，want %v", tc.addr, got, tc.want)
		}
	}
}

// TestAddressPolicy_AllowedNetworks 验证配置了允许网段时只允许网段内的地址。
func TestAddressPolicy_AllowedNetworks(t *testing.T) {
	policy := NewAddressPolicy([]netip.Prefix{
		netip.MustParsePrefix("10.20.0.0/16"),
		netip.MustParsePrefix("127.0.0.0/8"),
	})
	cases := []struct {
		addr string
		want bool
	}{
		{"10.20.3.4", true},
		{"127.0.0.1", true},
		{"::ffff:10.20.3.4", true},
		{"10.21.0.1", false},
		{"192.168.1.20", false},
		{"169.254.169.254", false},
		{"::1", false},
	}
	for _, tc := range cases {
		if got := policy.Allows(netip.MustParseAddr(tc.addr)); got != tc.want {
			t.Errorf("Allows(%s) = %v，want %v", tc.addr, got, tc.want)
		}
	}
}

// TestAddressPolicy_Control 验证拨号前的校验：拒绝时返回 ErrAddressNotAllowed。
func TestAddressPolicy_Control(t *testing.T) {
	var policy AddressPolicy
	if err := policy.control("tcp4", "10.0.0.5:8000", nil); err != nil {
		t.Fatalf("10.0.0.5 应被允许，实际 %v", err)
	}
	for _, address := range []string{"127.0.0.1:8000", "[::1]:8000", "[fe80::1%eth0]:8000", "not-an-address"} {
		if err := policy.control("tcp", address, nil); !errors.Is(err, ErrAddressNotAllowed) {
			t.Errorf("control(%q) 应返回 ErrAddressNotAllowed，实际 %v", address, err)
		}
	}
}
