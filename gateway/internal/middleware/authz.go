package middleware

import (
	"net/http"
	"strings"
)

// Authz 返回入口鉴权卸载中间件。
//
// 职责（首期骨架）：从租户身份请求头解析租户，注入 context 并向下游传播；
// 缺失租户身份时直接返回 401，阻止匿名请求进入业务链路。
//
// ────────────────────────────────────────────────────────────────────────
// 【零信任原则 — 必读】
//  1. 网关位于信任边界之外，仅做“入口鉴权卸载”：完成身份解析与基础校验，
//     并将解析结果（租户/主体/scope）以受信请求头下发给下游。
//  2. 下游控制平面【必须】对租户身份进行二次校验，不得仅凭网关转发的请求头
//     就信任租户——否则攻击者可绕过网关直接访问控制平面，或通过伪造头部实现
//     横向越权（confused deputy / header injection）。
//  3. 生产环境应在此处完成 JWT/OIDC 签名与有效期校验，并结合 mTLS 建立
//     网关与控制平面之间的相互信任；本骨架以“头存在性”作为占位实现。
//  4. 该中间件只解决“谁在调用”，不解决“能否调用”；细粒度授权（Policy/决策）
//     属于 Python 控制平面职责（决策 ≠ 授权）。
//
// ────────────────────────────────────────────────────────────────────────
func Authz(tenantHeader string) Middleware {
	if tenantHeader == "" {
		tenantHeader = DefaultTenantHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tenant := strings.TrimSpace(r.Header.Get(tenantHeader))
			if tenant == "" {
				http.Error(w, "401 Unauthorized: missing tenant identity", http.StatusUnauthorized)
				return
			}

			// TODO(生产): 校验 JWT/OIDC，解析 Subject/Scopes/TokenID，
			//             并按路由执行 Authorize 判定。
			//
			// 防御性覆写（WP-D1）：以受信解析值（TrimSpace 后的首个值）覆写入站
			// 租户头，消除多值头/空白填充歧义——下游转发链（反代、流式代理、
			// 限流 key）重新读取请求头时拿到的与鉴权上下文严格同源，客户端
			// 伪造的头部变体无法造成“鉴权身份 ≠ 转发身份”的横向越权。
			r.Header.Set(tenantHeader, tenant)
			ctx := WithTenant(r.Context(), tenant)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// AuthzWithToken 返回带 Bearer Token 校验的鉴权中间件。
//
// 行为：
//   - 若 validTokens 非空，则从 Authorization: Bearer <token> 头提取 token，
//     校验是否在 validTokens 集合中；缺失或无效时返回 401。
//   - 若 validTokens 为空，退化为仅检查租户头存在性（与 Authz 一致），
//     便于在未启用 Token 校验的环境无缝降级。
//   - 通过 Token 校验后，仍需校验租户头存在性，保证多租户语义不被绕过。
//
// 安全约束：Token 比较采用 map 查找（O(1)），不泄露有效 token 列表；
// 生产环境应结合 JWT 签名校验使用，本实现为静态 Token 白名单占位。
func AuthzWithToken(tenantHeader string, validTokens map[string]bool) Middleware {
	if tenantHeader == "" {
		tenantHeader = DefaultTenantHeader
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// 如果配置了 token 校验，先校验 Bearer Token。
			if len(validTokens) > 0 {
				token := extractBearerToken(r)
				if token == "" || !validTokens[token] {
					http.Error(w, "401 Unauthorized: invalid or missing token", http.StatusUnauthorized)
					return
				}
			}
			// 租户头校验（现有逻辑）。
			tenant := strings.TrimSpace(r.Header.Get(tenantHeader))
			if tenant == "" {
				http.Error(w, "401 Unauthorized: missing tenant identity", http.StatusUnauthorized)
				return
			}
			// 防御性覆写：与 Authz 一致，以受信解析值覆写入站租户头（见上方注释）。
			r.Header.Set(tenantHeader, tenant)
			ctx := WithTenant(r.Context(), tenant)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// extractBearerToken 从 Authorization 头提取 Bearer token。
//
// 仅识别 "Bearer <token>" 前缀格式（RFC 6750 §2.1），其他方案（Basic/Digest 等）
// 返回空字符串。token 前后空白会被 TrimSpace 去除，避免填充绕过。
func extractBearerToken(r *http.Request) string {
	auth := r.Header.Get("Authorization")
	if auth == "" {
		return ""
	}
	const prefix = "Bearer "
	if !strings.HasPrefix(auth, prefix) {
		return ""
	}
	return strings.TrimSpace(auth[len(prefix):])
}
