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
			ctx := WithTenant(r.Context(), tenant)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}