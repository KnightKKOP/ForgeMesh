package metricstest

import (
	"math"
	"testing"
)

func TestParse_AcceptsValidText(t *testing.T) {
	text := `# HELP demo_total Demo \\ counter\nline two.
# TYPE demo_total counter
demo_total{a="x\"y",b="\\\n"} 3
demo_total 1.5e+03
# HELP demo_seconds Demo histogram.
# TYPE demo_seconds histogram
demo_seconds_bucket{id="a",le="0.5"} 1
demo_seconds_bucket{id="a",le="+Inf"} 2
demo_seconds_sum{id="a"} 1.25
demo_seconds_count{id="a"} 2
# TYPE demo_items gauge
demo_items NaN
`
	samples := MustParse(t, text)
	want := map[string]float64{
		`demo_total{a="x\"y",b="\\\n"}`:        3,
		`demo_total`:                           1500,
		`demo_seconds_bucket{id="a",le="0.5"}`: 1,
		`demo_seconds_count{id="a"}`:           2,
		`demo_seconds_sum{id="a"}`:             1.25,
	}
	for key, value := range want {
		if got, ok := samples[key]; !ok || got != value {
			t.Errorf("%s = %v (存在 %v), want %v", key, got, ok, value)
		}
	}
	if got := samples["demo_items"]; !math.IsNaN(got) {
		t.Errorf("demo_items = %v, want NaN", got)
	}
}

func TestParse_RejectsInvalidText(t *testing.T) {
	cases := map[string]string{
		"缺少结尾换行":  "# TYPE a counter\na 1",
		"空行":      "# TYPE a counter\n\na 1\n",
		"多余的注释":   "# TYPE a counter\n# note\na 1\n",
		"未声明类型":   "a 1\n",
		"重复的类型":   "# TYPE a counter\na 1\n# TYPE a counter\n",
		"重复的说明":   "# HELP a x\n# HELP a y\n# TYPE a counter\na 1\n",
		"说明转义非法":  "# HELP a bad \\t escape\n# TYPE a counter\na 1\n",
		"不属于当前指标": "# TYPE a counter\nb 1\n",
		"标签转义非法":  "# TYPE a counter\na{x=\"\\t\"} 1\n",
		"标签名重复":   "# TYPE a counter\na{x=\"1\",x=\"2\"} 1\n",
		"标签缺少逗号":  "# TYPE a counter\na{x=\"1\"y=\"2\"} 1\n",
		"标签结尾逗号":  "# TYPE a counter\na{x=\"1\",} 1\n",
		"取值非法":    "# TYPE a counter\na one\n",
		"样本重复":    "# TYPE a counter\na{x=\"1\"} 1\na{x=\"1\"} 2\n",
		"桶缺少 le":  "# TYPE h histogram\nh_bucket 1\nh_count 1\n",
		"桶不递增": "# TYPE h histogram\nh_bucket{le=\"1\"} 1\nh_bucket{le=\"0.5\"} 1\n" +
			"h_bucket{le=\"+Inf\"} 1\nh_count 1\n",
		"累计值减少":            "# TYPE h histogram\nh_bucket{le=\"1\"} 2\nh_bucket{le=\"+Inf\"} 1\nh_count 1\n",
		"没有 +Inf 桶":        "# TYPE h histogram\nh_bucket{le=\"1\"} 1\nh_count 1\n",
		"+Inf 与 _count 不等": "# TYPE h histogram\nh_bucket{le=\"+Inf\"} 2\nh_count 1\n",
		"缺少 _count":        "# TYPE h histogram\nh_bucket{le=\"+Inf\"} 1\n",
	}
	for name, text := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse(text); err == nil {
				t.Fatalf("Parse(%q) 应失败", text)
			}
		})
	}
}
