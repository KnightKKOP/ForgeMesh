package middleware

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// okHandler 是一个始终返回 200 的下游处理器，用于验证中间件放行。
var okHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
})

const (
	keyTenantA  = "fmk_key-for-tenant-a"
	keyReadOnly = "fmk_read-only-key-for-tenant-a"
)

func digestOf(key string) string {
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

func newTestAPIKeyAuthenticator(t *testing.T) *APIKeyAuthenticator {
	t.Helper()
	authn, err := NewAPIKeyAuthenticator([]APIKey{
		{ID: "a", SHA256: digestOf(keyTenantA), TenantID: "tenant-a", Subject: "alice", Scopes: []string{ScopeRead, ScopeWrite}},
		{ID: "ro", SHA256: digestOf(keyReadOnly), TenantID: "tenant-a", Subject: "viewer", Scopes: []string{ScopeRead}},
	}, DefaultTenantHeader)
	if err != nil {
		t.Fatalf("NewAPIKeyAuthenticator 失败: %v", err)
	}
	return authn
}

// decodeError 解析错误信封，返回 error.code。
func decodeError(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("错误响应 Content-Type 应为 application/json，实际 %q", ct)
	}
	var body struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("错误响应不是错误信封: %v（body=%s）", err, rec.Body.String())
	}
	return body.Error.Code
}

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

// TestAuthenticate_APIKeyBindsTenant 验证有效 key 放行：主体注入 context，
// 租户头被覆写为 key 绑定的租户（客户端可以不发租户头）。
func TestAuthenticate_APIKeyBindsTenant(t *testing.T) {
	var got Principal
	var forwardedTenant string
	next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got, _ = PrincipalFromContext(r.Context())
		forwardedTenant = r.Header.Get(DefaultTenantHeader)
		w.WriteHeader(http.StatusOK)
	})
	h := Authenticate(newTestAPIKeyAuthenticator(t), DefaultTenantHeader)(next)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "Bearer "+keyTenantA)
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("有效 key 应放行(200)，实际 %d", rec.Code)
	}
	if got.TenantID != "tenant-a" || got.Subject != "alice" || !got.HasScope(ScopeWrite) {
		t.Fatalf("context 主体不符: %+v", got)
	}
	if forwardedTenant != "tenant-a" {
		t.Fatalf("租户头应被覆写为 key 绑定的 tenant-a，实际 %q", forwardedTenant)
	}
}

// TestAuthenticate_APIKeyRejectsInvalidCredentials 验证缺失、非 Bearer、空白与未知 key
// 均返回 401 错误信封并附 WWW-Authenticate，且不触达下游。
func TestAuthenticate_APIKeyRejectsInvalidCredentials(t *testing.T) {
	cases := map[string]string{
		"缺失 Authorization": "",
		"非 Bearer 方案":      "Basic " + keyTenantA,
		"空白凭证":             "Bearer   ",
		"未知 key":           "Bearer fmk_unknown",
	}
	for name, auth := range cases {
		t.Run(name, func(t *testing.T) {
			reached := false
			next := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached = true })
			h := Authenticate(newTestAPIKeyAuthenticator(t), DefaultTenantHeader)(next)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
			req.Header.Set(DefaultTenantHeader, "tenant-a")
			if auth != "" {
				req.Header.Set("Authorization", auth)
			}
			h.ServeHTTP(rec, req)

			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("应返回 401，实际 %d", rec.Code)
			}
			if rec.Header().Get("WWW-Authenticate") != bearerChallenge {
				t.Fatalf("401 应携带 WWW-Authenticate=%q，实际 %q", bearerChallenge, rec.Header().Get("WWW-Authenticate"))
			}
			if code := decodeError(t, rec); code != "UNAUTHENTICATED" {
				t.Fatalf("错误码应为 UNAUTHENTICATED，实际 %q", code)
			}
			if reached {
				t.Fatal("认证失败的请求不应触达下游")
			}
		})
	}
}

// TestAuthenticate_APIKeyRejectsForeignTenantHeader 验证租户头与 key 绑定的租户不符时
// 返回 403（含多值头中夹带他租户的情形），而不是被静默改写。
func TestAuthenticate_APIKeyRejectsForeignTenantHeader(t *testing.T) {
	for name, values := range map[string][]string{
		"他租户":     {"tenant-b"},
		"多值夹带他租户": {"tenant-a", "tenant-b"},
	} {
		t.Run(name, func(t *testing.T) {
			h := Authenticate(newTestAPIKeyAuthenticator(t), DefaultTenantHeader)(okHandler)

			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
			req.Header.Set("Authorization", "Bearer "+keyTenantA)
			for _, v := range values {
				req.Header.Add(DefaultTenantHeader, v)
			}
			h.ServeHTTP(rec, req)

			if rec.Code != http.StatusForbidden {
				t.Fatalf("租户头与凭证不符应 403，实际 %d", rec.Code)
			}
			if code := decodeError(t, rec); code != "FORBIDDEN" {
				t.Fatalf("错误码应为 FORBIDDEN，实际 %q", code)
			}
		})
	}
}

// TestAuthenticate_APIKeyAcceptsMatchingTenantHeader 验证与凭证一致（含空白填充）的租户头放行。
func TestAuthenticate_APIKeyAcceptsMatchingTenantHeader(t *testing.T) {
	h := Authenticate(newTestAPIKeyAuthenticator(t), DefaultTenantHeader)(okHandler)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set("Authorization", "bearer "+keyTenantA)
	req.Header.Set(DefaultTenantHeader, " tenant-a ")
	h.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("与凭证一致的租户头应放行，实际 %d", rec.Code)
	}
}

// TestAuthenticate_ErrorEnvelopeCarriesCorrelationID 验证认证失败的错误信封带关联 ID。
func TestAuthenticate_ErrorEnvelopeCarriesCorrelationID(t *testing.T) {
	h := Chain(okHandler,
		Correlation(DefaultCorrelationHeader),
		Authenticate(newTestAPIKeyAuthenticator(t), DefaultTenantHeader),
	)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultCorrelationHeader, "corr-401")
	h.ServeHTTP(rec, req)

	var body struct {
		Error struct {
			CorrelationID string `json:"correlationId"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil || body.Error.CorrelationID != "corr-401" {
		t.Fatalf("错误信封应带 correlationId=corr-401，实际 body=%s", rec.Body.String())
	}
}

// TestRequireScope 验证作用域检查：持有所需作用域放行，否则 403；缺少主体同样拒绝。
func TestRequireScope(t *testing.T) {
	authn := newTestAPIKeyAuthenticator(t)
	h := Authenticate(authn, DefaultTenantHeader)(RequireScope(ScopeWrite)(okHandler))

	for key, want := range map[string]int{keyTenantA: http.StatusOK, keyReadOnly: http.StatusForbidden} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/api/v1/resources", nil)
		req.Header.Set("Authorization", "Bearer "+key)
		h.ServeHTTP(rec, req)
		if rec.Code != want {
			t.Fatalf("key %s 期望 %d，实际 %d", key, want, rec.Code)
		}
	}

	rec := httptest.NewRecorder()
	RequireScope(ScopeRead)(okHandler).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("context 中没有主体时应 403，实际 %d", rec.Code)
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
		{"方案名大小写不敏感", "bearer abc123", "abc123"},
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
