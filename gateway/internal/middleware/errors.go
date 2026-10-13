package middleware

import (
	"encoding/json"
	"net/http"
)

// WriteError 输出与控制平面一致的错误信封：
//
//	{"error":{"code":"...","message":"...","correlationId":"...","traceId":"..."}}
//
// correlationId 与 traceId 取自 Correlation 与 TraceContext 中间件注入的 context，缺失时省略。
func WriteError(w http.ResponseWriter, r *http.Request, status int, code, message string) {
	writeEnvelope(w, status, code, message, CorrelationIDFromContext(r.Context()), TraceIDFromContext(r.Context()))
}

// writeEnvelope 写出错误信封，ID 为空时省略对应字段。处理器可能已设置 Content-Length，
// 与信封的长度不符，因此删除；nosniff 与 http.Error 一致。
func writeEnvelope(w http.ResponseWriter, status int, code, message, correlationID, traceID string) {
	type errorBody struct {
		Code          string `json:"code"`
		Message       string `json:"message"`
		CorrelationID string `json:"correlationId,omitempty"`
		TraceID       string `json:"traceId,omitempty"`
	}
	body := struct {
		Error errorBody `json:"error"`
	}{errorBody{
		Code:          code,
		Message:       message,
		CorrelationID: correlationID,
		TraceID:       traceID,
	}}

	h := w.Header()
	h.Del("Content-Length")
	h.Set("Content-Type", "application/json")
	h.Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	// 序列化失败只可能源于内部构造，忽略并保留已写出的状态码。
	_ = json.NewEncoder(w).Encode(body)
}
