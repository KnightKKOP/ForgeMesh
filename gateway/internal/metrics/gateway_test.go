package metrics

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics/metricstest"
)

func scrape(t *testing.T, h http.Handler) map[string]float64 {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	return metricstest.MustParse(t, rec.Body.String())
}

// TestGateway_NilRecordsNothing 验证未配置指标的组件可以照常调用记录方法。
func TestGateway_NilRecordsNothing(t *testing.T) {
	var g *Gateway
	g.ObserveRequest(RouteProbe, http.StatusOK, time.Millisecond)
	g.ObserveFirstByte(time.Millisecond)
	g.UpstreamError(UpstreamInstance, ErrorConnect)
	g.ResolverLookup(LookupHit)
}

// TestGateway_ExportsAllMetrics 验证全部网关指标的名称、标签与取值；还没有发生的事件以 0 或
// 只有 HELP / TYPE 的形式出现，抓取时的取值来自登记的回调。
func TestGateway_ExportsAllMetrics(t *testing.T) {
	g := NewGateway()
	rejected := uint64(3)
	g.WatchRateLimiter(func() uint64 { return rejected })
	g.WatchInstances(func() (int, int) { return 2, 1 })

	before := scrape(t, g)
	for _, key := range []string{
		`forgemesh_gateway_stream_first_byte_seconds_count`,
		`forgemesh_gateway_resolver_lookups_total{result="hit"}`,
		`forgemesh_gateway_resolver_lookups_total{result="miss"}`,
		`forgemesh_gateway_resolver_lookups_total{result="coalesced"}`,
	} {
		if v, ok := before[key]; !ok || v != 0 {
			t.Errorf("%s 应以 0 出现，实际 %v（存在=%v）", key, v, ok)
		}
	}

	g.ObserveRequest(RouteInference, http.StatusOK, 1500*time.Millisecond)
	g.ObserveRequest(RouteInference, http.StatusOK, 20*time.Millisecond)
	g.ObserveRequest(RouteProbe, http.StatusServiceUnavailable, time.Millisecond)
	g.ObserveFirstByte(300 * time.Millisecond)
	g.UpstreamError(UpstreamInstance, ErrorBody)
	g.ResolverLookup(LookupMiss)
	g.ResolverLookup(LookupHit)
	g.ResolverLookup(LookupHit)
	rejected = 5

	after := scrape(t, g)
	want := map[string]float64{
		`forgemesh_gateway_http_requests_total{route="inference",status="200"}`:            2,
		`forgemesh_gateway_http_requests_total{route="probe",status="503"}`:                1,
		`forgemesh_gateway_http_request_duration_seconds_count{route="inference"}`:         2,
		`forgemesh_gateway_http_request_duration_seconds_bucket{route="inference",le="1"}`: 1,
		`forgemesh_gateway_http_request_duration_seconds_sum{route="inference"}`:           1.52,
		`forgemesh_gateway_stream_first_byte_seconds_bucket{le="0.25"}`:                    0,
		`forgemesh_gateway_stream_first_byte_seconds_bucket{le="0.5"}`:                     1,
		`forgemesh_gateway_upstream_errors_total{upstream="instance",kind="body"}`:         1,
		`forgemesh_gateway_rate_limited_total`:                                             5,
		`forgemesh_gateway_resolver_lookups_total{result="hit"}`:                           2,
		`forgemesh_gateway_resolver_lookups_total{result="miss"}`:                          1,
		`forgemesh_gateway_instances{state="healthy"}`:                                     2,
		`forgemesh_gateway_instances{state="unhealthy"}`:                                   1,
	}
	for key, value := range want {
		if got, ok := after[key]; !ok || got != value {
			t.Errorf("%s = %v（存在=%v），期望 %v", key, got, ok, value)
		}
	}
}
