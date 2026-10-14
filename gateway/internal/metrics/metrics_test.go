package metrics

import (
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/metrics/metricstest"
)

// TestRegistry_RendersTextFormat 逐字核对输出：HELP 与标签取值的转义、序列按标签排序、
// 直方图的累计桶（上界含边界值）与 +Inf、_sum / _count，没有标签的指标从 0 开始输出。
func TestRegistry_RendersTextFormat(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("demo_requests_total", "Requests.\nSecond line with \\ backslash.", "route", "status")
	c.Inc("b", "200")
	c.Inc("a", "500")
	c.Add(2, "a", "200")
	h := r.NewHistogram("demo_duration_seconds", "Durations.", []float64{0.25, 1}, "route")
	for _, v := range []float64{0.25, 0.5, 3} {
		h.Observe(v, "a")
	}
	r.NewCounter("demo_plain_total", "Unlabeled.")
	r.NewHistogram("demo_plain_seconds", "Unlabeled histogram.", []float64{1})
	r.NewGaugeFunc("demo_items", "Items.", []string{"state"}, func() []Sample {
		return []Sample{{Labels: []string{"z"}, Value: 1.5}, {Labels: []string{"q\"\n\\"}, Value: 2}}
	})

	want := `# HELP demo_requests_total Requests.\nSecond line with \\ backslash.
# TYPE demo_requests_total counter
demo_requests_total{route="a",status="200"} 2
demo_requests_total{route="a",status="500"} 1
demo_requests_total{route="b",status="200"} 1
# HELP demo_duration_seconds Durations.
# TYPE demo_duration_seconds histogram
demo_duration_seconds_bucket{route="a",le="0.25"} 1
demo_duration_seconds_bucket{route="a",le="1"} 2
demo_duration_seconds_bucket{route="a",le="+Inf"} 3
demo_duration_seconds_sum{route="a"} 3.75
demo_duration_seconds_count{route="a"} 3
# HELP demo_plain_total Unlabeled.
# TYPE demo_plain_total counter
demo_plain_total 0
# HELP demo_plain_seconds Unlabeled histogram.
# TYPE demo_plain_seconds histogram
demo_plain_seconds_bucket{le="1"} 0
demo_plain_seconds_bucket{le="+Inf"} 0
demo_plain_seconds_sum 0
demo_plain_seconds_count 0
# HELP demo_items Items.
# TYPE demo_items gauge
demo_items{state="q\"\n\\"} 2
demo_items{state="z"} 1.5
`
	got := r.Render()
	if got != want {
		t.Fatalf("输出不符\nwant:\n%s\ngot:\n%s", want, got)
	}
	metricstest.MustParse(t, got)
}

// TestFormatValue 覆盖整数、小数、指数与特殊值的写法。
func TestFormatValue(t *testing.T) {
	cases := []struct {
		value float64
		want  string
	}{
		{0, "0"}, {3, "3"}, {-2, "-2"}, {0.005, "0.005"}, {1e-05, "1e-05"}, {1e20, "1e+20"},
		{math.Inf(1), "+Inf"}, {math.Inf(-1), "-Inf"}, {math.NaN(), "NaN"},
	}
	for _, tc := range cases {
		if got := formatValue(tc.value); got != tc.want {
			t.Errorf("formatValue(%v) = %q，期望 %q", tc.value, got, tc.want)
		}
	}
}

// TestRegistry_BoundsSeries 验证序列数上限：超出后新的标签组合计入溢出序列，总数不变；
// 已有序列照常累加。
func TestRegistry_BoundsSeries(t *testing.T) {
	r := newRegistry(2)
	c := r.NewCounter("demo_total", "Demo.", "id")
	h := r.NewHistogram("demo_seconds", "Demo.", []float64{1}, "id")
	for _, id := range []string{"a", "b", "c", "d", "a"} {
		c.Inc(id)
		h.Observe(0.5, id)
	}
	r.NewGaugeFunc("demo_items", "Demo.", []string{"id"}, func() []Sample {
		return []Sample{{[]string{"a"}, 1}, {[]string{"b"}, 2}, {[]string{"c"}, 3}, {[]string{"d"}, 4}}
	})

	samples := metricstest.MustParse(t, r.Render())
	want := map[string]float64{
		`demo_total{id="a"}`:                2,
		`demo_total{id="b"}`:                1,
		`demo_total{id="overflow"}`:         2,
		`demo_seconds_count{id="a"}`:        2,
		`demo_seconds_count{id="overflow"}`: 2,
		`demo_items{id="a"}`:                1,
		`demo_items{id="b"}`:                2,
		`demo_items{id="overflow"}`:         7,
	}
	for key, value := range want {
		if got, ok := samples[key]; !ok || got != value {
			t.Errorf("%s = %v（存在=%v），期望 %v", key, got, ok, value)
		}
	}
	if _, ok := samples[`demo_total{id="c"}`]; ok {
		t.Error("超出上限的标签组合不应单独成为序列")
	}
}

// TestRegistry_RejectsProgrammingErrors 验证非法定义与错误用法会 panic。
func TestRegistry_RejectsProgrammingErrors(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("demo_total", "Demo.", "route")
	h := r.NewHistogram("demo_seconds", "Demo.", DefaultBuckets)
	cases := map[string]func(){
		"非法指标名":     func() { r.NewCounter("1demo_total", "Demo.") },
		"非法标签名":     func() { r.NewCounter("demo_a_total", "Demo.", "bad-label") },
		"保留标签 le":   func() { r.NewHistogram("demo_b_seconds", "Demo.", DefaultBuckets, "le") },
		"双下划线标签":    func() { r.NewCounter("demo_c_total", "Demo.", "__name") },
		"重复标签名":     func() { r.NewCounter("demo_d_total", "Demo.", "a", "a") },
		"重复指标名":     func() { r.NewCounter("demo_total", "Demo.") },
		"没有桶":       func() { r.NewHistogram("demo_e_seconds", "Demo.", nil) },
		"桶不递增":      func() { r.NewHistogram("demo_f_seconds", "Demo.", []float64{1, 1}) },
		"+Inf 桶":    func() { r.NewHistogram("demo_g_seconds", "Demo.", []float64{1, math.Inf(1)}) },
		"标签个数不符":    func() { c.Inc("a", "b") },
		"计数器减少":     func() { c.Add(-1, "a") },
		"直方图记录 NaN": func() { h.Observe(math.NaN()) },
		"回调标签个数不符": func() {
			r.NewGaugeFunc("demo_h", "Demo.", []string{"a"}, func() []Sample { return []Sample{{Value: 1}} })
			r.Render()
		},
	}
	for name, fn := range cases {
		t.Run(name, func(t *testing.T) {
			defer func() {
				if recover() == nil {
					t.Fatal("应 panic")
				}
			}()
			fn()
		})
	}
}

// TestRegistry_ServeHTTP 验证 GET 输出文本格式、HEAD 只有响应头、其他方法 405。
func TestRegistry_ServeHTTP(t *testing.T) {
	r := NewRegistry()
	r.NewCounter("demo_total", "Demo.").Inc()

	get := httptest.NewRecorder()
	r.ServeHTTP(get, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if get.Code != http.StatusOK || get.Header().Get("Content-Type") != ContentType {
		t.Fatalf("GET 应 200 且为文本格式，实际 %d %q", get.Code, get.Header().Get("Content-Type"))
	}
	if !strings.Contains(get.Body.String(), "demo_total 1\n") {
		t.Fatalf("GET 应输出指标，实际 %q", get.Body.String())
	}

	head := httptest.NewRecorder()
	r.ServeHTTP(head, httptest.NewRequest(http.MethodHead, "/metrics", nil))
	if head.Code != http.StatusOK || head.Body.Len() != 0 || head.Header().Get("Content-Length") == "" {
		t.Fatalf("HEAD 应 200、无响应体且带 Content-Length，实际 %d %q", head.Code, head.Body.String())
	}

	post := httptest.NewRecorder()
	r.ServeHTTP(post, httptest.NewRequest(http.MethodPost, "/metrics", nil))
	if post.Code != http.StatusMethodNotAllowed || post.Header().Get("Allow") != "GET, HEAD" {
		t.Fatalf("POST 应 405 且带 Allow，实际 %d %q", post.Code, post.Header().Get("Allow"))
	}
}

// TestRegistry_ConcurrentUpdates 在 -race 下并发更新与输出，计数不丢失。
func TestRegistry_ConcurrentUpdates(t *testing.T) {
	r := NewRegistry()
	c := r.NewCounter("demo_total", "Demo.", "route")
	h := r.NewHistogram("demo_seconds", "Demo.", DefaultBuckets, "route")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 500; j++ {
				c.Inc("a")
				h.Observe(0.01, "a")
				if j%100 == 0 {
					_ = r.Render()
				}
			}
		}()
	}
	wg.Wait()

	samples := metricstest.MustParse(t, r.Render())
	if samples[`demo_total{route="a"}`] != 4000 || samples[`demo_seconds_count{route="a"}`] != 4000 {
		t.Fatalf("并发更新后计数应为 4000，实际 %v / %v",
			samples[`demo_total{route="a"}`], samples[`demo_seconds_count{route="a"}`])
	}
}
