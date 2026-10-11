package config

import (
	"strings"
	"testing"
)

// TestLoad_Defaults 验证未设置环境变量时使用安全默认值。
func TestLoad_Defaults(t *testing.T) {
	t.Setenv(envListenAddr, "")
	t.Setenv(envUpstreamURL, "")
	t.Setenv(envRateLimitRPS, "")
	t.Setenv(envRateLimitBurst, "")
	t.Setenv(envResolverTTLSeconds, "")
	t.Setenv(envUnhealthyCooldownSeconds, "")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 默认应成功: %v", err)
	}
	if cfg.ResolverTTLSeconds != defaultResolverTTLSeconds {
		t.Fatalf("ResolverTTLSeconds 默认应为 %d，实际 %d", defaultResolverTTLSeconds, cfg.ResolverTTLSeconds)
	}
	if cfg.UnhealthyCooldownSeconds != defaultUnhealthyCooldownSeconds {
		t.Fatalf("UnhealthyCooldownSeconds 默认应为 %d，实际 %d",
			defaultUnhealthyCooldownSeconds, cfg.UnhealthyCooldownSeconds)
	}
}

// TestLoad_ResolverOverrides 验证环境变量可覆盖解析器参数。
func TestLoad_ResolverOverrides(t *testing.T) {
	t.Setenv(envResolverTTLSeconds, "30")
	t.Setenv(envUnhealthyCooldownSeconds, "60")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 应成功: %v", err)
	}
	if cfg.ResolverTTLSeconds != 30 {
		t.Fatalf("ResolverTTLSeconds 应为 30，实际 %d", cfg.ResolverTTLSeconds)
	}
	if cfg.UnhealthyCooldownSeconds != 60 {
		t.Fatalf("UnhealthyCooldownSeconds 应为 60，实际 %d", cfg.UnhealthyCooldownSeconds)
	}
}

// TestLoad_InvalidResolverValues 验证非正数取值被拒绝。
func TestLoad_InvalidResolverValues(t *testing.T) {
	cases := []struct {
		name string
		env  string
		val  string
		want string
	}{
		{"TTL 零", envResolverTTLSeconds, "0", envResolverTTLSeconds},
		{"TTL 负数", envResolverTTLSeconds, "-5", envResolverTTLSeconds},
		{"TTL 非数字", envResolverTTLSeconds, "abc", envResolverTTLSeconds},
		{"冷却零", envUnhealthyCooldownSeconds, "0", envUnhealthyCooldownSeconds},
		{"冷却负数", envUnhealthyCooldownSeconds, "-1", envUnhealthyCooldownSeconds},
		{"冷却非数字", envUnhealthyCooldownSeconds, "x", envUnhealthyCooldownSeconds},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(tc.env, tc.val)
			_, err := Load()
			if err == nil {
				t.Fatalf("%s=%q 应返回错误", tc.env, tc.val)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("错误信息应提及 %s，实际: %v", tc.want, err)
			}
		})
	}
}
