// Package config 负责加载并校验 Go 网关的运行时配置。
//
// 设计约束（见 docs/architecture/development-plan.md §5.8）：
//   - 网关作为统一外部入口，仅承载入口关注点（认证卸载、限流、流式转发）。
//   - 配置全部来自环境变量，便于容器化部署；未设置项使用安全默认值。
//   - 本包仅依赖标准库，保证 `go build ./...` 无需第三方模块。
package config

import (
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
)

// 环境变量名。全部以 GATEWAY_ 前缀，避免与 Python 控制平面冲突。
const (
	envListenAddr               = "GATEWAY_LISTEN_ADDR"
	envUpstreamURL              = "GATEWAY_UPSTREAM_URL"
	envRateLimitRPS             = "GATEWAY_RATE_LIMIT_RPS"
	envRateLimitBurst           = "GATEWAY_RATE_LIMIT_BURST"
	envTenantHeader             = "GATEWAY_TENANT_HEADER"
	envCorrelationHeader        = "GATEWAY_CORRELATION_HEADER"
	envResolverTTLSeconds       = "GATEWAY_RESOLVER_TTL_SECONDS"
	envUnhealthyCooldownSeconds = "GATEWAY_UNHEALTHY_COOLDOWN_SECONDS"
	envAuthMode                 = "GATEWAY_AUTH_MODE"
	envAPIKeysFile              = "GATEWAY_API_KEYS_FILE"
)

// 认证模式（GATEWAY_AUTH_MODE）。
const (
	// AuthModeAPIKey 校验 `Authorization: Bearer <key>`，key 绑定租户（默认）。
	AuthModeAPIKey = "apikey"
	// AuthModeDev 直接信任租户请求头，只允许监听回环地址，仅供本地开发。
	AuthModeDev = "dev"
)

// 默认值。
const (
	defaultListenAddr               = ":8081"
	defaultUpstreamURL              = "http://127.0.0.1:8080"
	defaultRateLimitRPS             = 50.0
	defaultRateLimitBurst           = 100
	defaultTenantHeader             = "X-Tenant-ID"
	defaultCorrelationHeader        = "X-Correlation-ID"
	defaultResolverTTLSeconds       = 5
	defaultUnhealthyCooldownSeconds = 10
	defaultAuthMode                 = AuthModeAPIKey
)

// Config 描述网关运行所需的全部配置项。
type Config struct {
	// ListenAddr 网关监听地址，形如 ":8081"。
	ListenAddr string
	// UpstreamURL Python 控制平面 / 上游服务基地址，如 "http://control-plane:8080"。
	UpstreamURL string
	// RateLimitRPS 每租户每秒补充的令牌数（请求速率）。
	RateLimitRPS float64
	// RateLimitBurst 每租户令牌桶容量（瞬时突发上限）。
	RateLimitBurst int
	// TenantHeader 承载租户身份的请求头名称。
	TenantHeader string
	// CorrelationHeader 承载链路关联 ID 的请求头名称。
	CorrelationHeader string
	// ResolverTTLSeconds 服务→实例列表缓存的 TTL（秒），正整数。
	ResolverTTLSeconds int
	// UnhealthyCooldownSeconds 实例被摘流后的冷却时长（秒），正整数。
	UnhealthyCooldownSeconds int
	// AuthMode 入口认证模式：AuthModeAPIKey 或 AuthModeDev。
	AuthMode string
	// APIKeysFile AuthModeAPIKey 下的 key 文件路径（与控制平面 FORGEMESH_API_KEYS_FILE 格式相同）。
	APIKeysFile string
}

// Load 从环境变量读取配置；未设置项使用默认值。
// 返回错误表示环境变量存在但取值非法（如非数字、非正数）。
func Load() (Config, error) {
	cfg := Config{
		ListenAddr:        getEnv(envListenAddr, defaultListenAddr),
		UpstreamURL:       getEnv(envUpstreamURL, defaultUpstreamURL),
		TenantHeader:      getEnv(envTenantHeader, defaultTenantHeader),
		CorrelationHeader: getEnv(envCorrelationHeader, defaultCorrelationHeader),
	}

	rps, err := getFloatEnv(envRateLimitRPS, defaultRateLimitRPS)
	if err != nil {
		return Config{}, err
	}
	if rps <= 0 {
		return Config{}, fmt.Errorf("config: %s 必须为正数，当前为 %v", envRateLimitRPS, rps)
	}
	cfg.RateLimitRPS = rps

	burst, err := getIntEnv(envRateLimitBurst, defaultRateLimitBurst)
	if err != nil {
		return Config{}, err
	}
	if burst <= 0 {
		return Config{}, fmt.Errorf("config: %s 必须为正整数，当前为 %d", envRateLimitBurst, burst)
	}
	cfg.RateLimitBurst = burst

	ttlSec, err := getIntEnv(envResolverTTLSeconds, defaultResolverTTLSeconds)
	if err != nil {
		return Config{}, err
	}
	if ttlSec <= 0 {
		return Config{}, fmt.Errorf("config: %s 必须为正整数，当前为 %d", envResolverTTLSeconds, ttlSec)
	}
	cfg.ResolverTTLSeconds = ttlSec

	cooldownSec, err := getIntEnv(envUnhealthyCooldownSeconds, defaultUnhealthyCooldownSeconds)
	if err != nil {
		return Config{}, err
	}
	if cooldownSec <= 0 {
		return Config{}, fmt.Errorf("config: %s 必须为正整数，当前为 %d", envUnhealthyCooldownSeconds, cooldownSec)
	}
	cfg.UnhealthyCooldownSeconds = cooldownSec

	// 归一化上游地址：去掉结尾斜杠，避免与请求路径拼接时出现双斜杠。
	cfg.UpstreamURL = strings.TrimRight(strings.TrimSpace(cfg.UpstreamURL), "/")
	if cfg.UpstreamURL == "" {
		return Config{}, fmt.Errorf("config: %s 不能为空", envUpstreamURL)
	}
	if cfg.ListenAddr = strings.TrimSpace(cfg.ListenAddr); cfg.ListenAddr == "" {
		return Config{}, fmt.Errorf("config: %s 不能为空", envListenAddr)
	}

	cfg.AuthMode = strings.ToLower(strings.TrimSpace(getEnv(envAuthMode, defaultAuthMode)))
	cfg.APIKeysFile = strings.TrimSpace(os.Getenv(envAPIKeysFile))
	switch cfg.AuthMode {
	case AuthModeAPIKey:
		if cfg.APIKeysFile == "" {
			return Config{}, fmt.Errorf("config: %s=%s（默认）时必须通过 %s 指定 API key 文件；"+
				"%s=%s 仅供本地开发", envAuthMode, AuthModeAPIKey, envAPIKeysFile, envAuthMode, AuthModeDev)
		}
	case AuthModeDev:
		if !isLoopbackAddr(cfg.ListenAddr) {
			return Config{}, fmt.Errorf("config: %s=%s 信任租户请求头，只允许监听回环地址（如 127.0.0.1:8081），"+
				"当前 %s=%q", envAuthMode, AuthModeDev, envListenAddr, cfg.ListenAddr)
		}
	default:
		return Config{}, fmt.Errorf("config: %s 取值非法 %q（可选 %s、%s）",
			envAuthMode, cfg.AuthMode, AuthModeAPIKey, AuthModeDev)
	}

	return cfg, nil
}

// isLoopbackAddr 报告监听地址是否只绑定回环接口；未指定主机（如 ":8081"）表示绑定全部接口。
func isLoopbackAddr(addr string) bool {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return false
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func getEnv(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && strings.TrimSpace(v) != "" {
		return v
	}
	return fallback
}

func getFloatEnv(key string, fallback float64) (float64, error) {
	v, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(v) == "" {
		return fallback, nil
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		return 0, fmt.Errorf("config: %s 取值非法 %q: %w", key, v, err)
	}
	return f, nil
}

func getIntEnv(key string, fallback int) (int, error) {
	v, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(v) == "" {
		return fallback, nil
	}
	i, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil {
		return 0, fmt.Errorf("config: %s 取值非法 %q: %w", key, v, err)
	}
	return i, nil
}
