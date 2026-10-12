package config

import (
	"strings"
	"testing"
)

const testKeysFile = "/etc/forgemesh/api-keys.json"

// setAPIKeyAuthEnv 设置默认（apikey）认证模式所需的环境变量。
func setAPIKeyAuthEnv(t *testing.T) {
	t.Helper()
	t.Setenv(envAuthMode, "")
	t.Setenv(envAPIKeysFile, testKeysFile)
}

// TestLoad_Defaults 验证未设置环境变量时使用安全默认值。
func TestLoad_Defaults(t *testing.T) {
	t.Setenv(envListenAddr, "")
	t.Setenv(envUpstreamURL, "")
	t.Setenv(envRateLimitRPS, "")
	t.Setenv(envRateLimitBurst, "")
	t.Setenv(envResolverTTLSeconds, "")
	t.Setenv(envUnhealthyCooldownSeconds, "")
	t.Setenv(envMaxRequestBodyBytes, "")
	setAPIKeyAuthEnv(t)

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 默认应成功: %v", err)
	}
	if cfg.MaxRequestBodyBytes != DefaultMaxRequestBodyBytes {
		t.Fatalf("MaxRequestBodyBytes 默认应为 %d，实际 %d", DefaultMaxRequestBodyBytes, cfg.MaxRequestBodyBytes)
	}
	if cfg.ResolverTTLSeconds != defaultResolverTTLSeconds {
		t.Fatalf("ResolverTTLSeconds 默认应为 %d，实际 %d", defaultResolverTTLSeconds, cfg.ResolverTTLSeconds)
	}
	if cfg.UnhealthyCooldownSeconds != defaultUnhealthyCooldownSeconds {
		t.Fatalf("UnhealthyCooldownSeconds 默认应为 %d，实际 %d",
			defaultUnhealthyCooldownSeconds, cfg.UnhealthyCooldownSeconds)
	}
	if cfg.AuthMode != AuthModeAPIKey || cfg.APIKeysFile != testKeysFile {
		t.Fatalf("认证默认应为 apikey 且使用配置的 key 文件，实际 mode=%q file=%q", cfg.AuthMode, cfg.APIKeysFile)
	}
}

// TestLoad_AuthModes 验证认证配置：apikey 必须提供 key 文件；dev 只允许回环监听地址；
// 未知模式被拒绝。
func TestLoad_AuthModes(t *testing.T) {
	cases := []struct {
		name, mode, keysFile, listen string
		wantErr                      string
	}{
		{name: "apikey 缺少 key 文件", mode: "apikey", listen: ":8081", wantErr: envAPIKeysFile},
		{name: "默认模式缺少 key 文件", listen: ":8081", wantErr: envAPIKeysFile},
		{name: "未知模式", mode: "header", listen: "127.0.0.1:8081", wantErr: envAuthMode},
		{name: "dev 监听全部接口", mode: "dev", listen: ":8081", wantErr: envListenAddr},
		{name: "dev 监听 0.0.0.0", mode: "dev", listen: "0.0.0.0:8081", wantErr: envListenAddr},
		{name: "dev 监听内网地址", mode: "dev", listen: "10.0.0.5:8081", wantErr: envListenAddr},
		{name: "dev 监听 127.0.0.1", mode: "dev", listen: "127.0.0.1:8081"},
		{name: "dev 监听 localhost", mode: "DEV", listen: "localhost:8081"},
		{name: "dev 监听 ::1", mode: "dev", listen: "[::1]:8081"},
		{name: "apikey 监听全部接口", mode: "apikey", keysFile: testKeysFile, listen: ":8081"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envAuthMode, tc.mode)
			t.Setenv(envAPIKeysFile, tc.keysFile)
			t.Setenv(envListenAddr, tc.listen)

			cfg, err := Load()
			if tc.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("应返回提及 %s 的错误，实际 %v", tc.wantErr, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("Load 应成功: %v", err)
			}
			if cfg.AuthMode != strings.ToLower(tc.mode) {
				t.Fatalf("AuthMode 应为 %q，实际 %q", strings.ToLower(tc.mode), cfg.AuthMode)
			}
		})
	}
}

// TestLoad_ResolverOverrides 验证环境变量可覆盖解析器参数。
func TestLoad_ResolverOverrides(t *testing.T) {
	setAPIKeyAuthEnv(t)
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

// TestLoad_MaxRequestBodyBytesOverride 验证环境变量可覆盖请求体上限。
func TestLoad_MaxRequestBodyBytesOverride(t *testing.T) {
	setAPIKeyAuthEnv(t)
	t.Setenv(envMaxRequestBodyBytes, "1048576")

	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 应成功: %v", err)
	}
	if cfg.MaxRequestBodyBytes != 1<<20 {
		t.Fatalf("MaxRequestBodyBytes 应为 1048576，实际 %d", cfg.MaxRequestBodyBytes)
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
		{"请求体上限零", envMaxRequestBodyBytes, "0", envMaxRequestBodyBytes},
		{"请求体上限非数字", envMaxRequestBodyBytes, "10MiB", envMaxRequestBodyBytes},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			setAPIKeyAuthEnv(t)
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

// TestLoad_InstanceAllowedCIDRs 验证实例允许网段的解析：未设置为空，逗号分隔、忽略空白与空项，
// 网段按掩码归一化；非 CIDR 取值被拒绝。
func TestLoad_InstanceAllowedCIDRs(t *testing.T) {
	setAPIKeyAuthEnv(t)
	t.Setenv(envInstanceAllowedCIDRs, "")
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load 应成功: %v", err)
	}
	if len(cfg.InstanceAllowedCIDRs) != 0 {
		t.Fatalf("未设置时应为空，实际 %v", cfg.InstanceAllowedCIDRs)
	}

	t.Setenv(envInstanceAllowedCIDRs, " 10.20.0.0/16, 127.0.0.1/8 ,,fd00::/8")
	if cfg, err = Load(); err != nil {
		t.Fatalf("Load 应成功: %v", err)
	}
	got := make([]string, 0, len(cfg.InstanceAllowedCIDRs))
	for _, prefix := range cfg.InstanceAllowedCIDRs {
		got = append(got, prefix.String())
	}
	if want := "10.20.0.0/16 127.0.0.0/8 fd00::/8"; strings.Join(got, " ") != want {
		t.Fatalf("网段应为 %s，实际 %v", want, got)
	}

	t.Setenv(envInstanceAllowedCIDRs, "10.0.0.5")
	if _, err = Load(); err == nil || !strings.Contains(err.Error(), envInstanceAllowedCIDRs) {
		t.Fatalf("非 CIDR 取值应被拒绝并提及 %s，实际 %v", envInstanceAllowedCIDRs, err)
	}
}
