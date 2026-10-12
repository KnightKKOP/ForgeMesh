package middleware

import (
	"context"
	"errors"
	"net/http"
	"strings"
)

// 作用域，与控制平面（forgemesh.controlplane.auth）一致。
const (
	ScopeRead  = "read"
	ScopeWrite = "write"
	// ScopeAll 是通配作用域，持有者视为拥有全部作用域。
	ScopeAll = "*"
)

// bearerChallenge 是 401 响应携带的 WWW-Authenticate 质询（RFC 6750）。
const bearerChallenge = `Bearer realm="forgemesh"`

// Principal 是通过认证的调用主体。
type Principal struct {
	// Subject 主体标识（服务账号、用户等）。
	Subject string
	// TenantID 凭证绑定的租户，是多租户隔离的权威来源。
	TenantID string
	// Scopes 授予的作用域；含 ScopeAll 表示全部。
	Scopes []string
}

// HasScope 报告主体是否持有 scope（持有 ScopeAll 视为持有全部作用域）。
func (p Principal) HasScope(scope string) bool {
	for _, s := range p.Scopes {
		if s == ScopeAll || s == scope {
			return true
		}
	}
	return false
}

// Authenticator 校验请求凭证，返回调用主体。
//
// 失败时返回 *AuthError：Status 为 401（凭证缺失或无效）或 403（凭证有效但越权，
// 如租户头与凭证绑定的租户不一致）。
type Authenticator interface {
	Authenticate(r *http.Request) (Principal, error)
}

// AuthError 是认证失败；Message 不含凭证内容，可直接返回给客户端。
type AuthError struct {
	Status  int
	Message string
}

func (e *AuthError) Error() string { return e.Message }

func unauthenticated(message string) error {
	return &AuthError{Status: http.StatusUnauthorized, Message: message}
}

func forbidden(message string) error {
	return &AuthError{Status: http.StatusForbidden, Message: message}
}

// Authenticate 返回入口认证中间件。
//
// 校验通过后把调用主体注入 context，并以凭证绑定的租户覆写租户请求头：下游转发链
// （反代、流式代理）读到的租户与认证结果严格同源，客户端伪造的租户头变体（多值、
// 空白填充等）无法造成“认证身份 ≠ 转发身份”的越权。
//
// 控制平面不依赖网关的结论，会用同一份凭证独立校验（零信任，见 ADR-014）；网关只
// 回答“谁在调用”，细粒度授权仍属控制平面职责（决策 ≠ 授权）。
//
// 失败响应为错误信封：401 附 WWW-Authenticate，403 表示凭证有效但越权。
func Authenticate(authn Authenticator, tenantHeader string) Middleware {
	if authn == nil {
		panic("middleware: Authenticate 需要非空 Authenticator")
	}
	if tenantHeader == "" {
		tenantHeader = DefaultTenantHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			p, err := authn.Authenticate(r)
			if err != nil {
				writeAuthError(w, r, err)
				return
			}
			r.Header.Set(tenantHeader, p.TenantID)
			next.ServeHTTP(w, r.WithContext(WithPrincipal(r.Context(), p)))
		})
	}
}

// Authz 返回 dev 模式的认证中间件：直接信任租户请求头，不校验任何凭证。
//
// 仅用于本地开发与测试；生产环境使用 Authenticate 搭配 APIKeyAuthenticator。
func Authz(tenantHeader string) Middleware {
	return Authenticate(HeaderAuthenticator{TenantHeader: tenantHeader}, tenantHeader)
}

// RequireScope 返回要求调用主体持有 scope 的中间件，否则返回 403；须位于 Authenticate 之后。
func RequireScope(scope string) Middleware {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if p, ok := PrincipalFromContext(r.Context()); !ok || !p.HasScope(scope) {
				WriteError(w, r, http.StatusForbidden, "FORBIDDEN", "credentials lack the '"+scope+"' scope")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

func writeAuthError(w http.ResponseWriter, r *http.Request, err error) {
	var authErr *AuthError
	if !errors.As(err, &authErr) {
		authErr = &AuthError{Status: http.StatusUnauthorized, Message: "invalid credentials"}
	}
	if authErr.Status == http.StatusForbidden {
		WriteError(w, r, http.StatusForbidden, "FORBIDDEN", authErr.Message)
		return
	}
	w.Header().Set("WWW-Authenticate", bearerChallenge)
	WriteError(w, r, http.StatusUnauthorized, "UNAUTHENTICATED", authErr.Message)
}

// HeaderAuthenticator 是 dev 模式认证器：信任租户请求头（TrimSpace 后的首个值），
// 不校验任何凭证并授予全部作用域。只能用于本地开发与测试。
type HeaderAuthenticator struct {
	// TenantHeader 承载租户身份的请求头，为空时使用 DefaultTenantHeader。
	TenantHeader string
}

// Authenticate 实现 Authenticator。
func (a HeaderAuthenticator) Authenticate(r *http.Request) (Principal, error) {
	header := a.TenantHeader
	if header == "" {
		header = DefaultTenantHeader
	}
	tenant := strings.TrimSpace(r.Header.Get(header))
	if tenant == "" {
		return Principal{}, unauthenticated("missing tenant identity")
	}
	return Principal{Subject: "dev:" + tenant, TenantID: tenant, Scopes: []string{ScopeAll}}, nil
}

// WithPrincipal 把调用主体及其租户（见 TenantFromContext）写入 context。
func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return WithTenant(context.WithValue(ctx, principalKey, p), p.TenantID)
}

// PrincipalFromContext 返回 Authenticate 注入的调用主体。
func PrincipalFromContext(ctx context.Context) (Principal, bool) {
	if ctx == nil {
		return Principal{}, false
	}
	p, ok := ctx.Value(principalKey).(Principal)
	return p, ok
}

// extractBearerToken 从 Authorization 头提取 Bearer 凭证。
//
// 方案名大小写不敏感（RFC 7235 §2.1）；其他方案（Basic/Digest 等）返回空字符串。
func extractBearerToken(r *http.Request) string {
	scheme, token, ok := strings.Cut(strings.TrimSpace(r.Header.Get("Authorization")), " ")
	if !ok || !strings.EqualFold(scheme, "Bearer") {
		return ""
	}
	return strings.TrimSpace(token)
}
