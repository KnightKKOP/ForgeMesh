package middleware

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// errorEnvelope 是错误信封中 error 对象的字段。
type errorEnvelope struct {
	Code          string `json:"code"`
	Message       string `json:"message"`
	CorrelationID string `json:"correlationId"`
	TraceID       string `json:"traceId"`
}

// readEnvelope 解析错误信封（不允许信封之外的字段），并检查 Content-Type 与 nosniff。
func readEnvelope(t *testing.T, rec *httptest.ResponseRecorder) errorEnvelope {
	t.Helper()
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("错误响应 Content-Type 应为 application/json，实际 %q", ct)
	}
	if v := rec.Header().Get("X-Content-Type-Options"); v != "nosniff" {
		t.Fatalf("错误响应应带 X-Content-Type-Options: nosniff，实际 %q", v)
	}
	var body struct {
		Error *errorEnvelope `json:"error"`
	}
	dec := json.NewDecoder(bytes.NewReader(rec.Body.Bytes()))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&body); err != nil || body.Error == nil {
		t.Fatalf("响应体不是错误信封: %v（body=%s）", err, rec.Body.String())
	}
	return *body.Error
}

// TestRecovery_RespondsWithEnvelope 验证响应头写出之前的 panic 兜底为 500 INTERNAL_ERROR 错误
// 信封，不含 panic 的内容，处理器设置的 Content-Length 被删除；信封与日志带 Correlation、
// TraceContext（位于 Recovery 之内）确定的 ID，没有经过它们的请求不带 ID（不读取请求头）。
func TestRecovery_RespondsWithEnvelope(t *testing.T) {
	panicky := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "1000")
		panic("boom: 内部细节")
	})
	cases := map[string]struct {
		inner   []Middleware
		want    errorEnvelope
		wantLog string
	}{
		"经过 Correlation 与 TraceContext": {
			inner: []Middleware{Correlation(DefaultCorrelationHeader), TraceContext()},
			want: errorEnvelope{Code: "INTERNAL_ERROR", Message: "internal server error",
				CorrelationID: "corr-1", TraceID: testTraceID},
			wantLog: "panic recovered: corr=corr-1 trace=" + testTraceID +
				" method=POST path=/api/v1/resources err=boom: 内部细节\n",
		},
		"没有经过（探针与 /metrics）": {
			want:    errorEnvelope{Code: "INTERNAL_ERROR", Message: "internal server error"},
			wantLog: "panic recovered: corr= trace= method=POST path=/api/v1/resources err=boom: 内部细节\n",
		},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			var logs bytes.Buffer
			h := Recovery(log.New(&logs, "", 0))(Chain(panicky, tc.inner...))
			req := httptest.NewRequest(http.MethodPost, "/api/v1/resources", nil)
			req.Header.Set(DefaultCorrelationHeader, "corr-1")
			req.Header.Set(TraceparentHeader, testTraceparent)
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)

			if rec.Code != http.StatusInternalServerError {
				t.Fatalf("panic 应兜底为 500，实际 %d", rec.Code)
			}
			if got := readEnvelope(t, rec); got != tc.want {
				t.Fatalf("错误信封应为 %+v，实际 %+v", tc.want, got)
			}
			if cl := rec.Header().Get("Content-Length"); cl != "" {
				t.Fatalf("处理器设置的 Content-Length 应被删除，实际 %q", cl)
			}
			if strings.Contains(rec.Body.String(), "boom") {
				t.Fatalf("响应体不应包含 panic 的内容: %s", rec.Body.String())
			}
			if got := logs.String(); !strings.HasPrefix(got, tc.wantLog) || !strings.Contains(got, "goroutine ") {
				t.Fatalf("日志应以 %q 开头并带堆栈，实际 %q", tc.wantLog, got)
			}
		})
	}
}

// TestRecovery_AbortsStartedResponses 验证响应头写出之后的 panic 不再向响应末尾追加内容，而是以
// http.ErrAbortHandler 中止响应（net/http 不记录）：客户端读到已写出的部分后遇到 unexpected EOF。
// 处理器自己 panic(http.ErrAbortHandler)（如 ReverseProxy 复制响应体失败）时 Recovery 不记录日志。
func TestRecovery_AbortsStartedResponses(t *testing.T) {
	cases := map[string]struct {
		value   any
		wantLog bool
	}{
		"处理器 panic":                   {value: "boom", wantLog: true},
		"panic(http.ErrAbortHandler)": {value: http.ErrAbortHandler, wantLog: false},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			var logs, serverLogs bytes.Buffer
			srv := httptest.NewUnstartedServer(Recovery(log.New(&logs, "", 0))(
				http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
					w.Header().Set("Content-Type", "text/event-stream")
					_, _ = io.WriteString(w, "data: partial\n\n")
					w.(http.Flusher).Flush()
					panic(tc.value)
				})))
			srv.Config.ErrorLog = log.New(&serverLogs, "", 0)
			srv.Start()

			resp, err := srv.Client().Get(srv.URL)
			if err != nil {
				srv.Close()
				t.Fatalf("请求失败: %v", err)
			}
			body, readErr := io.ReadAll(resp.Body)
			resp.Body.Close()
			// Close 等待处理请求的 goroutine 结束，之后读取两份日志没有数据竞争。
			srv.Close()

			if resp.StatusCode != http.StatusOK || string(body) != "data: partial\n\n" {
				t.Fatalf("客户端应只收到已写出的 200 与部分响应体，实际 %d %q", resp.StatusCode, body)
			}
			if !errors.Is(readErr, io.ErrUnexpectedEOF) {
				t.Fatalf("响应应被中止（unexpected EOF），实际 %v", readErr)
			}
			if logged := strings.HasPrefix(logs.String(), "panic recovered: "); logged != tc.wantLog {
				t.Fatalf("Recovery 是否记录日志应为 %v，实际 %q", tc.wantLog, logs.String())
			}
			if serverLogs.Len() != 0 {
				t.Fatalf("应以 http.ErrAbortHandler 中止（net/http 不记录），实际 %q", serverLogs.String())
			}
		})
	}
}
