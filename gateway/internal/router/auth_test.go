// auth_test.go 从网关入口验证 apikey 认证模式（ADR-014）：
//
//   - 无凭证或凭证无效 → 401，不触达上游；
//   - 租户取自 key 绑定的租户，与凭证不符的租户头 → 403；
//   - 控制平面收到调用方凭证，可独立校验（含 Resolver 查询）；推理实例收不到；
//   - 推理调用要求 read 作用域。
package router

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
)

const (
	routerKeyTenantA  = "fmk_router-key-tenant-a"
	routerKeyWriteOne = "fmk_router-key-write-only"
)

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// apiKeyConfig 返回 apikey 模式配置，key 文件写入临时目录。
func apiKeyConfig(t *testing.T, upstreamURL string) config.Config {
	t.Helper()
	keys := fmt.Sprintf(`{"keys": [
		{"id": "a", "sha256": %q, "tenantId": "tenant-a", "subject": "alice"},
		{"id": "w", "sha256": %q, "tenantId": "tenant-a", "subject": "writer", "scopes": ["write"]}
	]}`, sha256Hex(routerKeyTenantA), sha256Hex(routerKeyWriteOne))
	path := filepath.Join(t.TempDir(), "api-keys.json")
	if err := os.WriteFile(path, []byte(keys), 0o600); err != nil {
		t.Fatalf("写入 key 文件失败: %v", err)
	}
	return config.Config{
		ListenAddr:        ":0",
		UpstreamURL:       upstreamURL,
		RateLimitRPS:      1000,
		RateLimitBurst:    1000,
		TenantHeader:      "X-Tenant-ID",
		CorrelationHeader: "X-Correlation-ID",
		AuthMode:          config.AuthModeAPIKey,
		APIKeysFile:       path,
	}
}

func newAPIKeyRouter(t *testing.T, upstreamURL string) *Router {
	t.Helper()
	r, err := New(apiKeyConfig(t, upstreamURL), log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	return r
}

// headerRecorder 是记录请求头的假上游。
type headerRecorder struct {
	mu      sync.Mutex
	calls   int
	headers http.Header
}

func (h *headerRecorder) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.mu.Lock()
	h.calls++
	h.headers = r.Header.Clone()
	h.mu.Unlock()
	w.WriteHeader(http.StatusOK)
	_, _ = io.WriteString(w, `{"items":[],"count":0}`)
}

func (h *headerRecorder) snapshot() (int, http.Header) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.calls, h.headers
}

func newRecordingUpstream(t *testing.T) (*headerRecorder, string) {
	t.Helper()
	rec := &headerRecorder{}
	srv := httptest.NewServer(rec)
	t.Cleanup(srv.Close)
	return rec, srv.URL
}

// TestAPIKeyMode_RejectsMissingAndUnknownCredentials 验证无凭证、仅租户头或未知 key 均 401，
// 且不触达控制平面。
func TestAPIKeyMode_RejectsMissingAndUnknownCredentials(t *testing.T) {
	upstreamRec, url := newRecordingUpstream(t)
	r := newAPIKeyRouter(t, url)

	for name, auth := range map[string]string{"无凭证": "", "未知 key": "Bearer fmk_unknown"} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/v1/inference-services", nil)
		req.Header.Set("X-Tenant-ID", "tenant-a")
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		r.ServeHTTP(rec, req)

		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s：应 401，实际 %d", name, rec.Code)
		}
		if rec.Header().Get("WWW-Authenticate") == "" {
			t.Fatalf("%s：401 应携带 WWW-Authenticate", name)
		}
	}
	if calls, _ := upstreamRec.snapshot(); calls != 0 {
		t.Fatalf("被拒请求不应触达控制平面，实际 %d 次", calls)
	}
}

// TestAPIKeyMode_ForwardsVerifiedTenantAndCredentials 验证租户取自凭证（客户端未发租户头），
// 且凭证原样转发给控制平面供其独立校验。
func TestAPIKeyMode_ForwardsVerifiedTenantAndCredentials(t *testing.T) {
	upstreamRec, url := newRecordingUpstream(t)
	r := newAPIKeyRouter(t, url)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/inference-services", nil)
	req.Header.Set("Authorization", "Bearer "+routerKeyTenantA)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("有效 key 应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	_, headers := upstreamRec.snapshot()
	if got := headers.Values("X-Tenant-ID"); len(got) != 1 || got[0] != "tenant-a" {
		t.Fatalf("控制平面收到的租户头应为 [tenant-a]，实际 %v", got)
	}
	if got := headers.Get("Authorization"); got != "Bearer "+routerKeyTenantA {
		t.Fatalf("控制平面应收到调用方凭证，实际 %q", got)
	}
}

// TestAPIKeyMode_RejectsForeignTenantHeader 验证凭证有效但租户头指向他租户时 403，不触达上游。
func TestAPIKeyMode_RejectsForeignTenantHeader(t *testing.T) {
	upstreamRec, url := newRecordingUpstream(t)
	r := newAPIKeyRouter(t, url)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/inference-services", nil)
	req.Header.Set("Authorization", "Bearer "+routerKeyTenantA)
	req.Header.Set("X-Tenant-ID", "tenant-b")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("伪造租户头应 403，实际 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"code":"FORBIDDEN"`) {
		t.Fatalf("403 应为 FORBIDDEN 错误信封，实际 %s", rec.Body.String())
	}
	if calls, _ := upstreamRec.snapshot(); calls != 0 {
		t.Fatalf("被拒请求不应触达控制平面，实际 %d 次", calls)
	}
}

// inferenceEnv 是假控制平面 + 假推理实例；两者都记录收到的 Authorization。
type inferenceEnv struct {
	router       *Router
	cpStatus     int
	cpHits       atomic.Int64
	cpAuth       atomic.Value
	instanceOK   atomic.Int64
	instanceAuth atomic.Value
}

func newInferenceEnv(t *testing.T, cpStatus int) *inferenceEnv {
	t.Helper()
	env := &inferenceEnv{cpStatus: cpStatus}
	instance := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		env.instanceOK.Add(1)
		env.instanceAuth.Store(r.Header.Get("Authorization") + r.Header.Get("Cookie"))
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: ok\n\n")
	}))
	t.Cleanup(instance.Close)
	cp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		env.cpHits.Add(1)
		env.cpAuth.Store(r.Header.Get("Authorization"))
		if env.cpStatus != http.StatusOK {
			w.WriteHeader(env.cpStatus)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, readyEndpointsJSON("Ready", []string{instance.URL}))
	}))
	t.Cleanup(cp.Close)
	env.router = newAPIKeyRouter(t, cp.URL)
	return env
}

func (e *inferenceEnv) chat(key string) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Cookie", "session=abc")
	e.router.ServeHTTP(rec, req)
	return rec
}

// TestAPIKeyMode_InferenceResolvesWithCallerCredentials 验证 Resolver 以调用方凭证查询控制平面，
// 而推理实例收不到调用方凭证。
func TestAPIKeyMode_InferenceResolvesWithCallerCredentials(t *testing.T) {
	env := newInferenceEnv(t, http.StatusOK)

	if rec := env.chat(routerKeyTenantA); rec.Code != http.StatusOK {
		t.Fatalf("推理请求应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	if got, _ := env.cpAuth.Load().(string); got != "Bearer "+routerKeyTenantA {
		t.Fatalf("Resolver 应携带调用方凭证查询控制平面，实际 %q", got)
	}
	if env.instanceOK.Load() != 1 {
		t.Fatalf("请求应转发到推理实例 1 次，实际 %d", env.instanceOK.Load())
	}
	if got, _ := env.instanceAuth.Load().(string); got != "" {
		t.Fatalf("推理实例不应收到调用方凭证，实际 %q", got)
	}
}

// TestAPIKeyMode_InferenceRequiresReadScope 验证缺少 read 作用域的 key 调用推理返回 403，
// 且不查询控制平面。
func TestAPIKeyMode_InferenceRequiresReadScope(t *testing.T) {
	env := newInferenceEnv(t, http.StatusOK)

	rec := env.chat(routerKeyWriteOne)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("缺少 read 作用域应 403，实际 %d", rec.Code)
	}
	if env.cpHits.Load() != 0 {
		t.Fatalf("被拒请求不应查询控制平面，实际 %d 次", env.cpHits.Load())
	}
}

// TestAPIKeyMode_ControlPlaneForbiddenMapsTo403 验证控制平面拒绝解析（403）时网关返回 403。
func TestAPIKeyMode_ControlPlaneForbiddenMapsTo403(t *testing.T) {
	env := newInferenceEnv(t, http.StatusForbidden)

	rec := env.chat(routerKeyTenantA)
	if rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), `"code":"FORBIDDEN"`) {
		t.Fatalf("控制平面 403 应映射为 403 FORBIDDEN，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	if env.instanceOK.Load() != 0 {
		t.Fatal("解析被拒时不应转发到推理实例")
	}
}

// TestNew_RejectsInvalidAuthConfig 验证认证配置缺失或非法时 New 失败（不回退为信任请求头）。
func TestNew_RejectsInvalidAuthConfig(t *testing.T) {
	valid := apiKeyConfig(t, "http://127.0.0.1:1")
	badFile := filepath.Join(t.TempDir(), "bad.json")
	if err := os.WriteFile(badFile, []byte(`{"keys": [{"id": "a"}]}`), 0o600); err != nil {
		t.Fatal(err)
	}

	cases := map[string]func(c *config.Config){
		"未设置模式":     func(c *config.Config) { c.AuthMode = "" },
		"未知模式":      func(c *config.Config) { c.AuthMode = "header" },
		"key 文件不存在": func(c *config.Config) { c.APIKeysFile = filepath.Join(t.TempDir(), "missing.json") },
		"key 文件非法":  func(c *config.Config) { c.APIKeysFile = badFile },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := valid
			mutate(&cfg)
			if _, err := New(cfg, log.New(io.Discard, "", 0)); err == nil {
				t.Fatal("New 应返回错误")
			}
		})
	}
}
