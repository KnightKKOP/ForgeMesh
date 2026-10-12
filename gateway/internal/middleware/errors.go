package middleware

import (
	"encoding/json"
	"net/http"
)

// WriteError 输出与控制平面一致的错误信封：
//
//	{"error":{"code":"...","message":"...","correlationId":"..."}}
//
// correlationId 取自 Correlation 中间件注入的 context，缺失时省略。
func WriteError(w http.ResponseWriter, r *http.Request, status int, code, message string) {
	type errorBody struct {
		Code          string `json:"code"`
		Message       string `json:"message"`
		CorrelationID string `json:"correlationId,omitempty"`
	}
	body := struct {
		Error errorBody `json:"error"`
	}{errorBody{Code: code, Message: message, CorrelationID: CorrelationIDFromContext(r.Context())}}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	// 序列化失败只可能源于内部构造，忽略并保留已写出的状态码。
	_ = json.NewEncoder(w).Encode(body)
}
