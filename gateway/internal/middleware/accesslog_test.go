package middleware

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"testing"
)

// serveLogged 经 Correlation → TraceContext → AccessLog 处理 req（外层 Recovery 兜底 panic），
// 返回响应与访问日志。
func serveLogged(t *testing.T, h http.Handler, req *http.Request) (*httptest.ResponseRecorder, string) {
	t.Helper()
	var buf bytes.Buffer
	chain := Recovery(log.New(io.Discard, "", 0))(Chain(h,
		Correlation(DefaultCorrelationHeader),
		TraceContext(),
		AccessLog(log.New(&buf, "", 0)),
	))
	rec := httptest.NewRecorder()
	chain.ServeHTTP(rec, req)
	return rec, buf.String()
}

// TestAccessLog_LogsRequestWithCorrelationAndTraceIDs 验证每个请求一行访问日志，带方法、路径、
// 状态码、耗时、关联 ID 与 trace-id。
func TestAccessLog_LogsRequestWithCorrelationAndTraceIDs(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/api/v1/workflows?limit=1", nil)
	req.Header.Set(DefaultCorrelationHeader, "corr-1")
	req.Header.Set(TraceparentHeader, testTraceparent)
	_, logged := serveLogged(t, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
	}), req)

	want := regexp.MustCompile(`^access method=POST path="/api/v1/workflows" status=201 ` +
		`duration=\S+ corr=corr-1 trace=` + testTraceID + "\n$")
	if !want.MatchString(logged) {
		t.Fatalf("访问日志不符: %q", logged)
	}
}

// TestAccessLog_QuotesThePath 验证路径加引号转义，解码出的换行不会伪造日志行。
func TestAccessLog_QuotesThePath(t *testing.T) {
	_, logged := serveLogged(t, http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}),
		httptest.NewRequest(http.MethodGet, "/api/v1/a%0Aaccess%20method=GET", nil))

	if strings.Count(logged, "\n") != 1 || !strings.Contains(logged, `path="/api/v1/a\naccess method=GET"`) {
		t.Fatalf("路径应转义且只输出一行: %q", logged)
	}
}

// TestAccessLog_LogsPanics 验证处理器 panic 时（Recovery 兜底）仍输出访问日志：响应头未写出时
// 记 500，已写出时记已写出的状态码。
func TestAccessLog_LogsPanics(t *testing.T) {
	cases := map[string]struct {
		handler http.HandlerFunc
		want    string
	}{
		"响应头未写出": {
			handler: func(http.ResponseWriter, *http.Request) { panic("boom") },
			want:    "status=500",
		},
		"响应头已写出": {
			handler: func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusAccepted)
				panic("boom")
			},
			want: "status=202",
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			rec, logged := serveLogged(t, tc.handler, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))
			if !strings.Contains(logged, tc.want) || !strings.Contains(logged, " trace=") {
				t.Fatalf("访问日志应含 %s: %q", tc.want, logged)
			}
			if name == "响应头未写出" && rec.Code != http.StatusInternalServerError {
				t.Fatalf("Recovery 应兜底为 500，实际 %d", rec.Code)
			}
		})
	}
}
