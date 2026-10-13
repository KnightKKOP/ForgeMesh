package middleware

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const (
	testTraceID     = "4bf92f3577b34da6a3ce929d0e0e4736"
	testParentID    = "00f067aa0ba902b7"
	testTraceparent = "00-" + testTraceID + "-" + testParentID + "-01"
)

// TestParseTraceparent_AcceptsValidHeaders 验证合法的版本 00 与更高版本（按前 55 个字符解析，
// 之后的内容以 "-" 分隔）。
func TestParseTraceparent_AcceptsValidHeaders(t *testing.T) {
	for _, value := range []string{
		testTraceparent,
		"cc-" + testTraceID + "-" + testParentID + "-01",
		"cc-" + testTraceID + "-" + testParentID + "-01-future-field",
	} {
		if traceID, ok := ParseTraceparent(value); !ok || traceID != testTraceID {
			t.Errorf("ParseTraceparent(%q) = %q, %v，want %q", value, traceID, ok, testTraceID)
		}
	}
}

// TestParseTraceparent_RejectsInvalidHeaders 验证不合法的 traceparent（与控制平面的规则一致）。
func TestParseTraceparent_RejectsInvalidHeaders(t *testing.T) {
	for _, value := range []string{
		"",
		testTraceparent[:len(testTraceparent)-1],
		" " + testTraceparent,
		"00-" + strings.ToUpper(testTraceID) + "-" + testParentID + "-01",
		"00-" + testTraceID + "-" + strings.ToUpper(testParentID) + "-01",
		"00-" + testTraceID + "-" + testParentID + "-0A",
		"00-" + testTraceID[:31] + "g-" + testParentID + "-01",
		"00_" + testTraceID + "_" + testParentID + "_01",
		"00-" + strings.Repeat("0", 32) + "-" + testParentID + "-01",
		"00-" + testTraceID + "-" + strings.Repeat("0", 16) + "-01",
		"ff-" + testTraceID + "-" + testParentID + "-01",
		testTraceparent + "-",
		testTraceparent + "-future",
		"cc-" + testTraceID + "-" + testParentID + "-01x",
	} {
		if traceID, ok := ParseTraceparent(value); ok {
			t.Errorf("ParseTraceparent(%q) 应不合法，实际得到 %q", value, traceID)
		}
	}
}

// TestNewTraceparent_IsValidUnsampledAndDistinct 验证生成的 traceparent 合法、版本 00、
// 未采样，且每次不同。
func TestNewTraceparent_IsValidUnsampledAndDistinct(t *testing.T) {
	first, firstID := newTraceparent()
	second, secondID := newTraceparent()

	if traceID, ok := ParseTraceparent(first); !ok || traceID != firstID {
		t.Fatalf("生成的 traceparent %q 应合法且 trace-id 为 %q", first, firstID)
	}
	if !strings.HasPrefix(first, "00-") || !strings.HasSuffix(first, "-00") {
		t.Fatalf("应为版本 00、flags 00，实际 %q", first)
	}
	if firstID == secondID || first[36:52] == second[36:52] {
		t.Fatalf("两次生成的 trace-id 与 parent-id 应不同: %q / %q", first, second)
	}
}

// TestNewTraceparent_NeverUsesAllZeroIDs 验证随机数全为零时重新生成 trace-id 与 parent-id。
func TestNewTraceparent_NeverUsesAllZeroIDs(t *testing.T) {
	values := []uint64{0, 0, 0, 2, 0, 3}
	original := randUint64
	t.Cleanup(func() { randUint64 = original })
	randUint64 = func() uint64 {
		v := values[0]
		values = values[1:]
		return v
	}

	traceparent, traceID := newTraceparent()
	if want := "00-00000000000000000000000000000002-0000000000000003-00"; traceparent != want {
		t.Fatalf("traceparent 应为 %q，实际 %q", want, traceparent)
	}
	if traceID != "00000000000000000000000000000002" {
		t.Fatalf("trace-id 不符: %q", traceID)
	}
}

// captureTrace 经 TraceContext 处理 req，返回下游看到的请求。
func captureTrace(t *testing.T, req *http.Request) *http.Request {
	t.Helper()
	var seen *http.Request
	TraceContext()(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { seen = r })).
		ServeHTTP(httptest.NewRecorder(), req)
	if seen == nil {
		t.Fatal("下游处理器应被调用")
	}
	return seen
}

// TestTraceContext_KeepsSingleValidTraceparent 验证恰好一个合法的 traceparent 原样沿用，
// tracestate 一并保留。
func TestTraceContext_KeepsSingleValidTraceparent(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(TraceparentHeader, testTraceparent)
	req.Header.Set(TracestateHeader, "vendor=opaque")

	seen := captureTrace(t, req)
	if got := seen.Header.Values(TraceparentHeader); len(got) != 1 || got[0] != testTraceparent {
		t.Fatalf("traceparent 应原样转发，实际 %v", got)
	}
	if got := seen.Header.Get(TracestateHeader); got != "vendor=opaque" {
		t.Fatalf("tracestate 应保留，实际 %q", got)
	}
	if got := TraceIDFromContext(seen.Context()); got != testTraceID {
		t.Fatalf("context 中的 trace-id 应为 %q，实际 %q", testTraceID, got)
	}
	if got := TraceparentFromContext(seen.Context()); got != testTraceparent {
		t.Fatalf("context 中的 traceparent 应为 %q，实际 %q", testTraceparent, got)
	}
}

// TestTraceContext_StartsNewTraceOtherwise 验证缺失、不合法或多个 traceparent 时开始新的追踪：
// 转发恰好一个新生成的 traceparent，丢弃 tracestate。
func TestTraceContext_StartsNewTraceOtherwise(t *testing.T) {
	cases := map[string][]string{
		"缺失":  nil,
		"不合法": {"00-" + strings.Repeat("0", 32) + "-" + testParentID + "-01"},
		"多个":  {testTraceparent, testTraceparent},
	}
	for name, inbound := range cases {
		t.Run(name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
			for _, value := range inbound {
				req.Header.Add(TraceparentHeader, value)
			}
			req.Header.Set(TracestateHeader, "vendor=opaque")

			seen := captureTrace(t, req)
			got := seen.Header.Values(TraceparentHeader)
			if len(got) != 1 {
				t.Fatalf("应转发恰好一个 traceparent，实际 %v", got)
			}
			traceID, ok := ParseTraceparent(got[0])
			if !ok || traceID == testTraceID {
				t.Fatalf("应转发新生成的合法 traceparent，实际 %q", got[0])
			}
			if TraceIDFromContext(seen.Context()) != traceID || TraceparentFromContext(seen.Context()) != got[0] {
				t.Fatal("context 中的追踪上下文应与转发的 traceparent 一致")
			}
			if _, present := seen.Header[http.CanonicalHeaderKey(TracestateHeader)]; present {
				t.Fatal("开始新的追踪时应丢弃 tracestate")
			}
		})
	}
}

// TestWriteError_IncludesCorrelationAndTraceIDs 验证错误信封带上 context 中的关联 ID 与
// trace-id，没有时省略这两个字段。
func TestWriteError_IncludesCorrelationAndTraceIDs(t *testing.T) {
	decode := func(rec *httptest.ResponseRecorder) map[string]any {
		var body struct {
			Error map[string]any `json:"error"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatalf("错误信封不是 JSON: %v", err)
		}
		return body.Error
	}
	writeUnauthorized := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		WriteError(w, r, http.StatusUnauthorized, "UNAUTHORIZED", "missing credentials")
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil)
	req.Header.Set(DefaultCorrelationHeader, "corr-1")
	req.Header.Set(TraceparentHeader, testTraceparent)
	Chain(writeUnauthorized, Correlation(DefaultCorrelationHeader), TraceContext()).ServeHTTP(rec, req)
	body := decode(rec)
	if body["correlationId"] != "corr-1" || body["traceId"] != testTraceID {
		t.Fatalf("错误信封应带关联 ID 与 trace-id，实际 %v", body)
	}

	rec = httptest.NewRecorder()
	writeUnauthorized.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/resources", nil))
	body = decode(rec)
	if _, ok := body["correlationId"]; ok {
		t.Fatalf("没有关联 ID 时应省略 correlationId，实际 %v", body)
	}
	if _, ok := body["traceId"]; ok {
		t.Fatalf("没有追踪上下文时应省略 traceId，实际 %v", body)
	}
}
