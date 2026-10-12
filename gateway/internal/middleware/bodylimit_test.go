package middleware

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestLimitRequestBody_DeclaredLengthOverLimit 验证声明长度超限时直接 413，不进入处理器。
func TestLimitRequestBody_DeclaredLengthOverLimit(t *testing.T) {
	called := false
	h := LimitRequestBody(8)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/resources", strings.NewReader("123456789")))

	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("超限应 413，实际 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"code":"REQUEST_TOO_LARGE"`) {
		t.Fatalf("错误体应含 REQUEST_TOO_LARGE，实际: %s", rec.Body.String())
	}
	if called {
		t.Fatal("超限请求不应进入处理器")
	}
}

// TestLimitRequestBody_ChunkedBodyOverLimit 验证未声明长度的请求体在读取超限时失败。
func TestLimitRequestBody_ChunkedBodyOverLimit(t *testing.T) {
	var readErr error
	h := LimitRequestBody(8)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, readErr = io.ReadAll(r.Body)
	}))

	req := httptest.NewRequest(http.MethodPost, "/api/v1/resources", strings.NewReader("123456789"))
	req.ContentLength = -1
	h.ServeHTTP(httptest.NewRecorder(), req)

	var tooLarge *http.MaxBytesError
	if !errors.As(readErr, &tooLarge) || tooLarge.Limit != 8 {
		t.Fatalf("读取超限应返回 limit=8 的 *http.MaxBytesError，实际 %v", readErr)
	}
}

// TestLimitRequestBody_WithinLimit 验证未超限的请求体完整到达处理器，无请求体的请求原样放行。
func TestLimitRequestBody_WithinLimit(t *testing.T) {
	var got string
	var noBody bool
	h := LimitRequestBody(8)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		noBody = r.Body == http.NoBody
		b, _ := io.ReadAll(r.Body)
		got = string(b)
	}))

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/x", strings.NewReader("12345678")))
	if got != "12345678" {
		t.Fatalf("未超限的请求体应完整到达，实际 %q", got)
	}

	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/x", nil))
	if !noBody {
		t.Fatal("无请求体的请求应保持 http.NoBody")
	}
}
