package middleware

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"
)

// APIKey 是 key 文件中的一条记录，只保存 key 的 SHA-256 摘要，不保存明文。
//
// key 文件与控制平面（FORGEMESH_API_KEYS_FILE）共用同一格式与校验规则：
//
//	{"keys": [{"id": "acme-ci", "sha256": "<64 位十六进制>", "tenantId": "acme",
//	           "subject": "ci-bot", "scopes": ["read", "write"]}]}
//
// 新 key 由 `python -m forgemesh.controlplane.auth keygen` 生成。
type APIKey struct {
	ID       string
	SHA256   string
	TenantID string
	Subject  string
	Scopes   []string
}

// apiKeyRecord 是 APIKey 的 JSON 形态；scopes 保留原文，以区分缺省（取默认值）与 null（非法）。
type apiKeyRecord struct {
	ID       string          `json:"id"`
	SHA256   string          `json:"sha256"`
	TenantID string          `json:"tenantId"`
	Subject  string          `json:"subject"`
	Scopes   json.RawMessage `json:"scopes"`
}

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// LoadAPIKeys 读取并校验 key 文件：未知字段、非法摘要、未知作用域与多余内容均视为错误。
func LoadAPIKeys(path string) ([]APIKey, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("auth: 无法读取 API key 文件: %w", err)
	}
	keys, err := parseAPIKeys(raw)
	if err != nil {
		return nil, fmt.Errorf("auth: API key 文件 %s 非法: %w", path, err)
	}
	return keys, nil
}

func parseAPIKeys(raw []byte) ([]APIKey, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var file struct {
		Keys []apiKeyRecord `json:"keys"`
	}
	if err := dec.Decode(&file); err != nil {
		return nil, err
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, errors.New("JSON 文档之后存在多余内容")
	}
	if file.Keys == nil {
		return nil, errors.New(`缺少 "keys" 数组`)
	}
	keys := make([]APIKey, 0, len(file.Keys))
	for i, rec := range file.Keys {
		key, err := rec.toAPIKey()
		if err != nil {
			return nil, fmt.Errorf("keys[%d]: %w", i, err)
		}
		keys = append(keys, key)
	}
	return keys, nil
}

func (rec apiKeyRecord) toAPIKey() (APIKey, error) {
	digest := strings.ToLower(strings.TrimSpace(rec.SHA256))
	switch {
	case rec.ID == "":
		return APIKey{}, errors.New(`"id" 不能为空`)
	case !digestPattern.MatchString(digest):
		return APIKey{}, errors.New(`"sha256" 必须是 64 位十六进制摘要`)
	case rec.TenantID == "":
		return APIKey{}, errors.New(`"tenantId" 不能为空`)
	case rec.Subject == "":
		return APIKey{}, errors.New(`"subject" 不能为空`)
	}

	scopes := []string{ScopeRead, ScopeWrite}
	if rec.Scopes != nil {
		var declared []string
		if err := json.Unmarshal(rec.Scopes, &declared); err != nil || declared == nil {
			return APIKey{}, errors.New(`"scopes" 必须是字符串数组`)
		}
		scopes = declared
	}
	for _, s := range scopes {
		if s != ScopeRead && s != ScopeWrite && s != ScopeAll {
			return APIKey{}, fmt.Errorf("未知作用域 %q（可选 %s、%s、%s）", s, ScopeRead, ScopeWrite, ScopeAll)
		}
	}
	return APIKey{ID: rec.ID, SHA256: digest, TenantID: rec.TenantID, Subject: rec.Subject, Scopes: scopes}, nil
}

// APIKeyAuthenticator 校验 `Authorization: Bearer <key>`：key 绑定租户、主体与作用域。
//
// 租户请求头可以省略；若携带，每个值都必须与 key 绑定的租户一致，否则返回 403，
// 请求不会被静默改写到另一个租户。
type APIKeyAuthenticator struct {
	byDigest     map[string]APIKey
	tenantHeader string
}

// NewAPIKeyAuthenticator 构造认证器；重复的 id 或摘要视为配置错误。
func NewAPIKeyAuthenticator(keys []APIKey, tenantHeader string) (*APIKeyAuthenticator, error) {
	if tenantHeader == "" {
		tenantHeader = DefaultTenantHeader
	}
	byDigest := make(map[string]APIKey, len(keys))
	ids := make(map[string]struct{}, len(keys))
	for _, k := range keys {
		if _, dup := byDigest[k.SHA256]; dup {
			return nil, fmt.Errorf("auth: API key 摘要重复（id=%q）", k.ID)
		}
		if _, dup := ids[k.ID]; dup {
			return nil, fmt.Errorf("auth: API key id 重复: %q", k.ID)
		}
		byDigest[k.SHA256] = k
		ids[k.ID] = struct{}{}
	}
	return &APIKeyAuthenticator{byDigest: byDigest, tenantHeader: tenantHeader}, nil
}

// Authenticate 实现 Authenticator。
func (a *APIKeyAuthenticator) Authenticate(r *http.Request) (Principal, error) {
	token := extractBearerToken(r)
	if token == "" {
		return Principal{}, unauthenticated("missing bearer credentials")
	}
	// 查表的键是 key 的 SHA-256 摘要而非 key 本身，查表耗时不会泄露 key 的内容。
	sum := sha256.Sum256([]byte(token))
	key, ok := a.byDigest[hex.EncodeToString(sum[:])]
	if !ok {
		return Principal{}, unauthenticated("invalid credentials")
	}
	for _, claimed := range r.Header.Values(a.tenantHeader) {
		if c := strings.TrimSpace(claimed); c != "" && c != key.TenantID {
			return Principal{}, forbidden(a.tenantHeader + " does not match the tenant bound to the credentials")
		}
	}
	return Principal{
		Subject:  key.Subject,
		TenantID: key.TenantID,
		Scopes:   append([]string(nil), key.Scopes...),
	}, nil
}
