package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// okHandler 是一个始终返回 200 的下游处理器，用于验证中间件放行。
var okHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
})

// TestAuthz_MissingTenantHeader 验证缺失租户头时返回 401。
func TestAuthz_MissingTenantHeader(t *testing.T) {
	h := Authz(DefaultTenantHeader)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺失租户头应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthz_ValidTenantHeader 验证携带租户头时放行并注入 context。
func TestAuthz_ValidTenantHeader(t *testing.T) {
	var ctxTenant string
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctxTenant = TenantFromContext(r.Context())
		w.WriteHeader(http.StatusOK)
	})
	h := Authz(DefaultTenantHeader)(next)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("有效租户头应返回 200，实际 %d", rec.Code)
	}
	if ctxTenant != "tenant-a" {
		t.Fatalf("context 租户应为 tenant-a，实际 %q", ctxTenant)
	}
}

// TestAuthz_DefaultHeader 验证空 tenantHeader 参数时回退到默认头名。
func TestAuthz_DefaultHeader(t *testing.T) {
	h := Authz("")(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("默认头名下应放行，实际 %d", rec.Code)
	}
}

// TestAuthz_BlankTenantHeader 验证纯空白租户头被视为缺失。
func TestAuthz_BlankTenantHeader(t *testing.T) {
	h := Authz(DefaultTenantHeader)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "   ")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("纯空白租户头应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_ValidToken 验证有效 Bearer Token + 租户头放行。
func TestAuthzWithToken_ValidToken(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Bearer secret-token-123")
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("有效 token 应放行(200)，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_InvalidToken 验证无效 token 返回 401。
func TestAuthzWithToken_InvalidToken(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Bearer wrong-token")
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("无效 token 应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_MissingToken 验证缺失 Authorization 头返回 401。
func TestAuthzWithToken_MissingToken(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺失 token 应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_EmptyTokenMap_Degraded 验证 validTokens 为空时退化为仅检查租户头。
func TestAuthzWithToken_EmptyTokenMap_Degraded(t *testing.T) {
	h := AuthzWithToken(DefaultTenantHeader, nil)(okHandler)

	// 无 Authorization 头但有租户头 → 应放行（降级模式）。
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("空 token map 降级模式下应放行(200)，实际 %d", rec.Code)
	}

	// 无租户头 → 仍应 401。
	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	h.ServeHTTP(rec2, req2)

	if rec2.Code != http.StatusUnauthorized {
		t.Fatalf("降级模式下缺失租户头应 401，实际 %d", rec2.Code)
	}
}

// TestAuthzWithToken_NonBearerScheme 验证非 Bearer 方案（如 Basic）被拒绝。
func TestAuthzWithToken_NonBearerScheme(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Basic dXNlcjpwYXNz")
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("非 Bearer 方案应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_BlankToken 验证 Bearer 后空白 token 被拒绝。
func TestAuthzWithToken_BlankToken(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Bearer   ")
	req.Header.Set(DefaultTenantHeader, "tenant-a")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("空白 token 应返回 401，实际 %d", rec.Code)
	}
}

// TestAuthzWithToken_ValidTokenMissingTenant 验证 token 有效但缺失租户头仍返回 401。
func TestAuthzWithToken_ValidTokenMissingTenant(t *testing.T) {
	tokens := map[string]bool{"secret-token-123": true}
	h := AuthzWithToken(DefaultTenantHeader, tokens)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Bearer secret-token-123")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("有效 token 但缺失租户头应 401，实际 %d", rec.Code)
	}
}

// TestExtractBearerToken_Table 验证 extractBearerToken 的各种输入。
func TestExtractBearerToken_Table(t *testing.T) {
	cases := []struct {
		name string
		auth string
		want string
	}{
		{"标准 Bearer", "Bearer abc123", "abc123"},
		{"带前后空白", "Bearer   abc123  ", "abc123"},
		{"缺失 Authorization", "", ""},
		{"非 Bearer 方案", "Basic abc123", ""},
		{"小写 bearer 不识别", "bearer abc123", ""},
		{"仅前缀无 token", "Bearer ", ""},
		{"Bearer 后多空格", "Bearer    xyz", "xyz"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/", nil)
			if tc.auth != "" {
				req.Header.Set("Authorization", tc.auth)
			}
			got := extractBearerToken(req)
			if got != tc.want {
				t.Fatalf("extractBearerToken(%q) = %q, want %q", tc.auth, got, tc.want)
			}
		})
	}
}
