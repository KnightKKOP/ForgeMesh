// tenant_security_test.go 验证多租户推理路径的安全语义（WP-D1）：
//
//   - 认证上下文绑定租户后，同租户访问授权服务成功；
//   - 跨租户访问被拒（按实现语义：网关透传租户身份，控制平面执行隔离 → 404）；
//   - 伪造防护：客户端伪造的 X-Tenant-ID 变体（多值/空白填充）经网关后
//     被规范化为受信单值，无法造成“鉴权身份 ≠ 转发身份”的横向越权；
//   - 无认证请求访问推理路径被 authz 拒绝，且不触达上游。
//
// 已知骨架限制（非本测试可消除）：Authz 尚未接入 JWT/OIDC 签名校验，
// 租户身份本质仍是“客户端声明 + 控制平面二次校验”（零信任，见 authz.go）；
// 本文件针对的是其中可立即利用的“头部变体歧义”攻击面。
package router

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
)

// tenantProbe 记录假控制平面（含推理实例）收到的租户头，供断言。
type tenantProbe struct {
	mu          sync.Mutex
	tenantHdr   string   // 承载租户身份的头名（默认 X-Tenant-ID）
	tenantVals  []string // 完整的租户头值列表（含多值头场景）
	calls       int
	lastPath    string
	lastCorr    string
	ownerTenant string // 模拟资源属主租户；请求租户不同则 404（控制平面隔离语义）
}

func newTenantProbe(owner string) *tenantProbe {
	return &tenantProbe{ownerTenant: owner, tenantHdr: "X-Tenant-ID"}
}

func (p *tenantProbe) handle(w http.ResponseWriter, r *http.Request) {
	p.mu.Lock()
	p.calls++
	p.lastPath = r.URL.Path
	p.lastCorr = r.Header.Get("X-Correlation-ID")
	p.tenantVals = append(p.tenantVals, r.Header.Values(p.tenantHdr)...)
	tenant := r.Header.Get(p.tenantHdr)
	owner := p.ownerTenant
	p.mu.Unlock()

	// 模拟控制平面 get_tenant_id 的租户隔离语义：资源属主与请求租户
	// 不一致时返回 404（不泄露资源存在性）。
	if owner != "" && tenant != owner {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"not found"}`))
		return
	}
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(`{"ok":true}`))
}

func (p *tenantProbe) snapshot() (vals []string, calls int, path, corr string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]string(nil), p.tenantVals...), p.calls, p.lastPath, p.lastCorr
}

// newTenantTestEnv 启动假控制平面并组装指向它的网关。
func newTenantTestEnv(t *testing.T, owner string) (*Router, *tenantProbe) {
	t.Helper()
	probe := newTenantProbe(owner)
	cp := httptest.NewServer(http.HandlerFunc(probe.handle))
	t.Cleanup(cp.Close)
	return newTestRouter(t, cp.URL), probe
}

// TestTenantSecurity_SameTenantAuthorizedAccess 验证同租户访问授权服务成功，
// 且网关向控制平面透传的租户身份与客户端声明一致。
func TestTenantSecurity_SameTenantAuthorizedAccess(t *testing.T) {
	r, probe := newTenantTestEnv(t, "tenant-a")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources/res-1", nil)
	req.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("同租户访问应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	vals, calls, _, _ := probe.snapshot()
	if calls != 1 {
		t.Fatalf("请求应触达控制平面 1 次，实际 %d", calls)
	}
	if len(vals) != 1 || vals[0] != "tenant-a" {
		t.Fatalf("控制平面收到的租户头应为 [tenant-a]，实际 %v", vals)
	}
}

// TestTenantSecurity_CrossTenantDeniedByControlPlane 验证跨租户访问被拒：
// 网关透传身份，控制平面按 get_tenant_id 语义隔离（非属主 → 404），
// 属主租户同路径可访问（证明 404 来自租户隔离而非路径不存在）。
func TestTenantSecurity_CrossTenantDeniedByControlPlane(t *testing.T) {
	r, _ := newTenantTestEnv(t, "tenant-a")

	// tenant-b 访问 tenant-a 的资源 → 404。
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources/res-1", nil)
	req.Header.Set("X-Tenant-ID", "tenant-b")
	r.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨租户访问应 404，实际 %d", rec.Code)
	}

	// tenant-a 访问同一路径 → 200。
	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/resources/res-1", nil)
	req2.Header.Set("X-Tenant-ID", "tenant-a")
	r.ServeHTTP(rec2, req2)
	if rec2.Code != http.StatusOK {
		t.Fatalf("属主租户访问应 200，实际 %d", rec2.Code)
	}
}

// TestTenantSecurity_SpoofedMultiValueHeaderNotEscalating 验证伪造防护（多值头）：
// 客户端重复发送 X-Tenant-ID（tenant-a、tenant-b）冒充他租户时，网关以受信
// 解析值（首个值）覆写后转发——控制平面只见到单一 tenant-a，多值歧义被消除。
//
// 修复前（Authz 只读不写）：两条值原样转发，若下游以“末值/合并”语义解析
// 即以 tenant-b 身份执行 → 横向越权。本用例为该修复的回归测试。
func TestTenantSecurity_SpoofedMultiValueHeaderNotEscalating(t *testing.T) {
	r, probe := newTenantTestEnv(t, "")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Add("X-Tenant-ID", "tenant-a") // 鉴权取首个值
	req.Header.Add("X-Tenant-ID", "tenant-b") // 伪造的第二个值
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("带有效租户头的请求应 200，实际 %d（body=%s）", rec.Code, rec.Body.String())
	}
	vals, _, _, _ := probe.snapshot()
	if len(vals) != 1 || vals[0] != "tenant-a" {
		t.Fatalf("转发到控制平面的租户头应为单一受信值 [tenant-a]，实际 %v（多值歧义未消除）", vals)
	}
}

// TestTenantSecurity_SpoofedWhitespaceHeaderNormalized 验证伪造防护（空白填充）：
// 客户端发送带空白的租户值时，鉴权上下文为 TrimSpace 后的值，转发头同样
// 被规范化为无空白单值，两者严格同源。
func TestTenantSecurity_SpoofedWhitespaceHeaderNormalized(t *testing.T) {
	r, probe := newTenantTestEnv(t, "")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("X-Tenant-ID", "  tenant-a  ")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("带（空白填充）租户头的请求应 200，实际 %d", rec.Code)
	}
	vals, _, _, _ := probe.snapshot()
	if len(vals) != 1 || vals[0] != "tenant-a" {
		t.Fatalf("转发租户头应为规范化后的 [tenant-a]，实际 %v", vals)
	}
}

// TestTenantSecurity_StreamPathRequiresAuth 验证无认证请求访问推理路径
// 被 authz 拒绝（401），且不触达控制平面（0 次调用）。
func TestTenantSecurity_StreamPathRequiresAuth(t *testing.T) {
	r, probe := newTenantTestEnv(t, "")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("无租户身份访问推理路径应 401，实际 %d", rec.Code)
	}
	if _, calls, _, _ := probe.snapshot(); calls != 0 {
		t.Fatalf("被拒请求不应触达控制平面，实际调用 %d 次", calls)
	}
}

// TestTenantSecurity_UnauthorizedResponseOpaque 验证认证失败响应不含内部细节：
// 固定文案、无堆栈/源码路径泄漏；关联 ID 已由 Correlation 中间件注入（其在
// Authz 之前执行），便于客户端报障定位。
func TestTenantSecurity_UnauthorizedResponseOpaque(t *testing.T) {
	r, _ := newTenantTestEnv(t, "")

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/inference/svc-a/chat", nil)
	r.ServeHTTP(rec, req)

	body := rec.Body.String()
	if want := "401 Unauthorized: missing tenant identity\n"; body != want {
		t.Fatalf("401 响应体应为固定文案 %q，实际 %q", want, body)
	}
	for _, leak := range []string{"goroutine", "panic", ".go:", "runtime", "stack"} {
		if strings.Contains(strings.ToLower(body), strings.ToLower(leak)) {
			t.Fatalf("401 响应体泄漏内部细节 %q: %s", leak, body)
		}
	}
	if rec.Header().Get("X-Correlation-ID") == "" {
		t.Fatal("401 响应仍应携带 X-Correlation-ID（Correlation 先于 Authz 执行）")
	}
}

// TestTenantSecurity_ConfigTenantHeaderEndToEnd 验证自定义租户头名配置
// 端到端生效：网关按配置的头名解析并覆写转发（默认 X-Tenant-ID 之外的别名）。
func TestTenantSecurity_ConfigTenantHeaderEndToEnd(t *testing.T) {
	probe := newTenantProbe("")
	cp := httptest.NewServer(http.HandlerFunc(probe.handle))
	t.Cleanup(cp.Close)

	cfg := config.Config{
		ListenAddr:        ":0",
		UpstreamURL:       cp.URL,
		RateLimitRPS:      1000,
		RateLimitBurst:    1000,
		TenantHeader:      "X-Org-Scope",
		CorrelationHeader: "X-Correlation-ID",
	}
	r, err := New(cfg, nil)
	if err != nil {
		t.Fatalf("New 失败: %v", err)
	}
	probe.tenantHdr = "X-Org-Scope" // probe 需按同一头名记录

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Add("X-Org-Scope", "tenant-x")
	req.Header.Add("X-Org-Scope", "tenant-y")
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("自定义租户头请求应 200，实际 %d", rec.Code)
	}
	vals, _, _, _ := probe.snapshot()
	if len(vals) != 1 || vals[0] != "tenant-x" {
		t.Fatalf("自定义头名下转发值应为单一 [tenant-x]，实际 %v", vals)
	}
}
