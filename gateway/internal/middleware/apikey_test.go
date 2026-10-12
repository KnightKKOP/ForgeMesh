package middleware

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func writeKeyFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "api-keys.json")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatalf("写入 key 文件失败: %v", err)
	}
	return path
}

// TestLoadAPIKeys_NormalizesDigestAndDefaultsScopes 验证摘要规范化为小写、
// 缺省 scopes 时授予 read/write、显式空数组表示不授予任何作用域。
func TestLoadAPIKeys_NormalizesDigestAndDefaultsScopes(t *testing.T) {
	digest := digestOf(keyTenantA)
	path := writeKeyFile(t, `{"keys": [
		{"id": "a", "sha256": " `+strings.ToUpper(digest)+` ", "tenantId": "tenant-a", "subject": "alice"},
		{"id": "none", "sha256": "`+strings.Repeat("0", 64)+`", "tenantId": "tenant-a", "subject": "s", "scopes": []}
	]}`)

	keys, err := LoadAPIKeys(path)
	if err != nil {
		t.Fatalf("LoadAPIKeys 失败: %v", err)
	}
	want := []APIKey{
		{ID: "a", SHA256: digest, TenantID: "tenant-a", Subject: "alice", Scopes: []string{ScopeRead, ScopeWrite}},
		{ID: "none", SHA256: strings.Repeat("0", 64), TenantID: "tenant-a", Subject: "s", Scopes: []string{}},
	}
	if !reflect.DeepEqual(keys, want) {
		t.Fatalf("解析结果不符:\nwant %+v\ngot  %+v", want, keys)
	}
}

// TestLoadAPIKeys_RejectsInvalidFiles 验证与控制平面一致的严格校验。
func TestLoadAPIKeys_RejectsInvalidFiles(t *testing.T) {
	zero := strings.Repeat("0", 64)
	cases := map[string]string{
		"非 JSON":          `not json`,
		"缺少 keys":         `{}`,
		"keys 为 null":     `{"keys": null}`,
		"顶层未知字段":          `{"keys": [], "extra": 1}`,
		"多余内容":            `{"keys": []} {}`,
		"摘要长度非法":          `{"keys": [{"id": "a", "sha256": "abc", "tenantId": "t", "subject": "s"}]}`,
		"缺少 subject":      `{"keys": [{"id": "a", "sha256": "` + zero + `", "tenantId": "t"}]}`,
		"缺少 id":           `{"keys": [{"sha256": "` + zero + `", "tenantId": "t", "subject": "s"}]}`,
		"snake_case 租户字段": `{"keys": [{"id": "a", "sha256": "` + zero + `", "tenant_id": "t", "subject": "s"}]}`,
		"未知作用域":           `{"keys": [{"id": "a", "sha256": "` + zero + `", "tenantId": "t", "subject": "s", "scopes": ["admin"]}]}`,
		"scopes 为 null":   `{"keys": [{"id": "a", "sha256": "` + zero + `", "tenantId": "t", "subject": "s", "scopes": null}]}`,
		"scopes 不是数组":     `{"keys": [{"id": "a", "sha256": "` + zero + `", "tenantId": "t", "subject": "s", "scopes": "read"}]}`,
	}
	for name, content := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := LoadAPIKeys(writeKeyFile(t, content)); err == nil {
				t.Fatalf("内容 %s 应被拒绝", content)
			}
		})
	}
}

// TestLoadAPIKeys_MissingFile 验证文件不可读时报错。
func TestLoadAPIKeys_MissingFile(t *testing.T) {
	if _, err := LoadAPIKeys(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("文件不存在应返回错误")
	}
}

// TestNewAPIKeyAuthenticator_RejectsDuplicates 验证重复的摘要或 id 视为配置错误。
func TestNewAPIKeyAuthenticator_RejectsDuplicates(t *testing.T) {
	a := APIKey{ID: "a", SHA256: digestOf(keyTenantA), TenantID: "t", Subject: "s"}
	for name, keys := range map[string][]APIKey{
		"重复摘要":  {a, {ID: "b", SHA256: a.SHA256, TenantID: "t", Subject: "s"}},
		"重复 id": {a, {ID: "a", SHA256: digestOf(keyReadOnly), TenantID: "t", Subject: "s"}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := NewAPIKeyAuthenticator(keys, ""); err == nil {
				t.Fatal("应返回错误")
			}
		})
	}
}
