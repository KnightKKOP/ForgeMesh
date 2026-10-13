// Package metrics 以 Prometheus 文本格式（0.0.4）输出网关指标，只使用标准库。
//
//   - 指标名与标签名在创建时校验，非法时 panic（属于编程错误）；每个指标的标签名固定，
//     输出时序列按标签取值排序。
//   - 标签基数有上限：调用方只传取值有限的标签（路由类别、枚举、状态码），不传租户、服务名、
//     路径或实例地址。作为兜底，一个指标最多 DefaultMaxSeries 个序列，之后出现的新标签组合
//     计入全部标签都取 OverflowLabelValue 的溢出序列（总数仍然准确）。
//   - 抓取时才确定取值的指标（如实例健康状态）由回调在每次输出时提供。
//   - 并发安全：更新与输出都持锁；输出时调用回调不持有任何指标的锁。
package metrics

import (
	"fmt"
	"math"
	"net/http"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// ContentType 是 /metrics 响应的 Content-Type（Prometheus 文本格式 0.0.4）。
const ContentType = "text/plain; version=0.0.4; charset=utf-8"

// DefaultMaxSeries 是每个指标默认最多的序列数。
const DefaultMaxSeries = 200

// OverflowLabelValue 是溢出序列的标签取值。
const OverflowLabelValue = "overflow"

// DefaultBuckets 是默认的直方图桶上界（秒），与 Prometheus 客户端库的默认值相同。
var DefaultBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10}

var (
	metricNamePattern = regexp.MustCompile(`^[a-zA-Z_:][a-zA-Z0-9_:]*$`)
	labelNamePattern  = regexp.MustCompile(`^[a-zA-Z_][a-zA-Z0-9_]*$`)
	// reservedLabels 由文本格式的直方图与摘要使用，不能再用作标签名。
	reservedLabels = map[string]bool{"le": true, "quantile": true}

	helpEscaper  = strings.NewReplacer(`\`, `\\`, "\n", `\n`)
	labelEscaper = strings.NewReplacer(`\`, `\\`, "\n", `\n`, `"`, `\"`)
)

// Sample 是回调在抓取时提供的一个序列；Labels 与指标的标签名按位置对应。
type Sample struct {
	Labels []string
	Value  float64
}

// desc 是指标的名称、说明、类型与标签名。
type desc struct {
	name      string
	help      string
	kind      string
	labels    []string
	maxSeries int
}

func (d *desc) writeHeader(b *strings.Builder) {
	fmt.Fprintf(b, "# HELP %s %s\n# TYPE %s %s\n", d.name, helpEscaper.Replace(d.help), d.name, d.kind)
}

// checkLabels 校验标签取值的个数，返回序列键。
func (d *desc) checkLabels(values []string) string {
	if len(values) != len(d.labels) {
		panic(fmt.Sprintf("metrics: %s 需要标签 %v，实际为 %q", d.name, d.labels, values))
	}
	return strings.Join(values, "\xff")
}

func (d *desc) overflow() []string {
	values := make([]string, len(d.labels))
	for i := range values {
		values[i] = OverflowLabelValue
	}
	return values
}

// labeled 是一个序列：标签取值与数据。
type labeled[S any] struct {
	values []string
	data   S
}

// sortedSeries 返回按标签取值排序的序列。
func sortedSeries[S any](series map[string]*labeled[S]) []*labeled[S] {
	out := make([]*labeled[S], 0, len(series))
	for _, s := range series {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return slices.Compare(out[i].values, out[j].values) < 0 })
	return out
}

// family 保存一个指标的全部序列，并实施序列数上限。
type family[S any] struct {
	desc
	mu     sync.Mutex
	series map[string]*labeled[S]
}

func newFamily[S any](d desc) family[S] {
	return family[S]{desc: d, series: make(map[string]*labeled[S])}
}

// at 返回 values 对应的序列，必要时创建；达到上限后新的标签组合返回溢出序列。调用方须持有 f.mu。
func (f *family[S]) at(values []string) *labeled[S] {
	key := f.checkLabels(values)
	if s, ok := f.series[key]; ok {
		return s
	}
	if len(f.series) >= f.maxSeries {
		values = f.overflow()
		key = strings.Join(values, "\xff")
		if s, ok := f.series[key]; ok {
			return s
		}
	}
	s := &labeled[S]{values: slices.Clone(values)}
	f.series[key] = s
	return s
}

// Counter 是只增不减的计数器（名称按约定以 _total 结尾）。
type Counter struct {
	family[float64]
}

// Inc 把 labels 对应的序列加 1。
func (c *Counter) Inc(labels ...string) { c.Add(1, labels...) }

// Add 把 labels 对应的序列加 v（v 不能为负）；Add(0, ...) 让序列以 0 出现在输出中。
func (c *Counter) Add(v float64, labels ...string) {
	if v < 0 || math.IsNaN(v) {
		panic(fmt.Sprintf("metrics: 计数器 %s 不能增加 %v", c.name, v))
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.at(labels).data += v
}

func (c *Counter) write(b *strings.Builder) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.writeHeader(b)
	for _, s := range sortedSeries(c.series) {
		writeSample(b, c.name, c.labels, s.values, "", "", s.data)
	}
}

// histogramData 是直方图一个序列的数据：counts 为各桶（最后一个是 +Inf）的非累计次数。
type histogramData struct {
	counts []uint64
	sum    float64
	count  uint64
}

// Histogram 按桶上界（含上界）累计观测次数，并记录总和与总次数。
type Histogram struct {
	family[histogramData]
	buckets []float64
}

// Observe 记录一次观测。
func (h *Histogram) Observe(v float64, labels ...string) {
	if math.IsNaN(v) {
		panic(fmt.Sprintf("metrics: 直方图 %s 不能记录 NaN", h.name))
	}
	index := sort.SearchFloat64s(h.buckets, v)
	h.mu.Lock()
	defer h.mu.Unlock()
	s := h.at(labels)
	if s.data.counts == nil {
		s.data.counts = make([]uint64, len(h.buckets)+1)
	}
	s.data.counts[index]++
	s.data.count++
	s.data.sum += v
}

func (h *Histogram) write(b *strings.Builder) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.writeHeader(b)
	for _, s := range sortedSeries(h.series) {
		counts := s.data.counts
		if counts == nil {
			counts = make([]uint64, len(h.buckets)+1)
		}
		var cumulative uint64
		for i, observed := range counts {
			cumulative += observed
			bound := math.Inf(1)
			if i < len(h.buckets) {
				bound = h.buckets[i]
			}
			writeSample(b, h.name+"_bucket", h.labels, s.values, "le", formatValue(bound), float64(cumulative))
		}
		writeSample(b, h.name+"_sum", h.labels, s.values, "", "", s.data.sum)
		writeSample(b, h.name+"_count", h.labels, s.values, "", "", float64(s.data.count))
	}
}

// funcMetric 的取值在每次输出时由回调提供。
type funcMetric struct {
	desc
	collect func() []Sample
}

func (f *funcMetric) write(b *strings.Builder) {
	samples := f.collect()
	values := make(map[string]*labeled[float64], len(samples))
	for _, sample := range samples {
		key := f.checkLabels(sample.Labels)
		s, ok := values[key]
		if !ok {
			labels := sample.Labels
			if len(values) >= f.maxSeries {
				labels = f.overflow()
				key = strings.Join(labels, "\xff")
				s, ok = values[key]
			}
			if !ok {
				s = &labeled[float64]{values: slices.Clone(labels)}
				values[key] = s
			}
		}
		s.data += sample.Value
	}
	f.writeHeader(b)
	for _, s := range sortedSeries(values) {
		writeSample(b, f.name, f.labels, s.values, "", "", s.data)
	}
}

type metric interface {
	write(b *strings.Builder)
}

// Registry 是一组指标，按创建顺序输出；实现 http.Handler（GET / HEAD）。
type Registry struct {
	maxSeries int

	mu      sync.Mutex
	names   map[string]bool
	metrics []metric
}

// NewRegistry 返回空的指标集合。
func NewRegistry() *Registry { return newRegistry(DefaultMaxSeries) }

func newRegistry(maxSeries int) *Registry {
	return &Registry{maxSeries: maxSeries, names: make(map[string]bool)}
}

func (r *Registry) newDesc(name, help, kind string, labels []string) desc {
	if !metricNamePattern.MatchString(name) {
		panic(fmt.Sprintf("metrics: 指标名非法 %q", name))
	}
	seen := make(map[string]bool, len(labels))
	for _, label := range labels {
		if !labelNamePattern.MatchString(label) || strings.HasPrefix(label, "__") || reservedLabels[label] {
			panic(fmt.Sprintf("metrics: 指标 %s 的标签名非法 %q", name, label))
		}
		if seen[label] {
			panic(fmt.Sprintf("metrics: 指标 %s 的标签名重复 %q", name, label))
		}
		seen[label] = true
	}
	return desc{name: name, help: help, kind: kind, labels: slices.Clone(labels), maxSeries: r.maxSeries}
}

func (r *Registry) register(name string, m metric) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.names[name] {
		panic(fmt.Sprintf("metrics: 指标 %s 已登记", name))
	}
	r.names[name] = true
	r.metrics = append(r.metrics, m)
}

// NewCounter 创建并登记一个计数器。没有标签的计数器从 0 开始出现在输出中。
func (r *Registry) NewCounter(name, help string, labels ...string) *Counter {
	c := &Counter{newFamily[float64](r.newDesc(name, help, "counter", labels))}
	if len(labels) == 0 {
		c.Add(0)
	}
	r.register(name, c)
	return c
}

// NewHistogram 创建并登记一个直方图；buckets 为严格递增的有限上界，+Inf 桶自动追加。
// 没有标签的直方图从 0 开始出现在输出中。
func (r *Registry) NewHistogram(name, help string, buckets []float64, labels ...string) *Histogram {
	if len(buckets) == 0 {
		panic(fmt.Sprintf("metrics: 直方图 %s 至少需要一个桶", name))
	}
	for i, bound := range buckets {
		if math.IsInf(bound, 0) || math.IsNaN(bound) || (i > 0 && bound <= buckets[i-1]) {
			panic(fmt.Sprintf("metrics: 直方图 %s 的桶上界必须有限且严格递增: %v", name, buckets))
		}
	}
	h := &Histogram{
		family:  newFamily[histogramData](r.newDesc(name, help, "histogram", labels)),
		buckets: slices.Clone(buckets),
	}
	if len(labels) == 0 {
		h.mu.Lock()
		h.at(nil)
		h.mu.Unlock()
	}
	r.register(name, h)
	return h
}

// NewCounterFunc 登记一个计数器，取值在每次输出时由 collect 提供（如组件自己维护的累计值）。
func (r *Registry) NewCounterFunc(name, help string, labels []string, collect func() []Sample) {
	r.register(name, &funcMetric{desc: r.newDesc(name, help, "counter", labels), collect: collect})
}

// NewGaugeFunc 登记一个仪表盘，取值在每次输出时由 collect 提供。
func (r *Registry) NewGaugeFunc(name, help string, labels []string, collect func() []Sample) {
	r.register(name, &funcMetric{desc: r.newDesc(name, help, "gauge", labels), collect: collect})
}

// Render 按文本格式输出全部指标的当前取值。
func (r *Registry) Render() string {
	r.mu.Lock()
	metrics := slices.Clone(r.metrics)
	r.mu.Unlock()
	var b strings.Builder
	for _, m := range metrics {
		m.write(&b)
	}
	return b.String()
}

// ServeHTTP 以文本格式响应 GET / HEAD，其他方法返回 405。
func (r *Registry) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	if req.Method != http.MethodGet && req.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		http.Error(w, http.StatusText(http.StatusMethodNotAllowed), http.StatusMethodNotAllowed)
		return
	}
	body := r.Render()
	w.Header().Set("Content-Type", ContentType)
	w.Header().Set("Content-Length", strconv.Itoa(len(body)))
	w.WriteHeader(http.StatusOK)
	if req.Method == http.MethodGet {
		_, _ = w.Write([]byte(body))
	}
}

func writeSample(b *strings.Builder, name string, labels, values []string, extraLabel, extraValue string, v float64) {
	b.WriteString(name)
	if len(labels) > 0 || extraLabel != "" {
		b.WriteByte('{')
		for i, label := range labels {
			if i > 0 {
				b.WriteByte(',')
			}
			fmt.Fprintf(b, `%s="%s"`, label, labelEscaper.Replace(values[i]))
		}
		if extraLabel != "" {
			if len(labels) > 0 {
				b.WriteByte(',')
			}
			fmt.Fprintf(b, `%s="%s"`, extraLabel, labelEscaper.Replace(extraValue))
		}
		b.WriteByte('}')
	}
	b.WriteByte(' ')
	b.WriteString(formatValue(v))
	b.WriteByte('\n')
}

// formatValue 把样本值写成文本格式：整数不带小数点，特殊值为 +Inf / -Inf / NaN。
func formatValue(v float64) string {
	switch {
	case math.IsNaN(v):
		return "NaN"
	case math.IsInf(v, 1):
		return "+Inf"
	case math.IsInf(v, -1):
		return "-Inf"
	case v == math.Trunc(v) && math.Abs(v) < 1e15:
		return strconv.FormatFloat(v, 'f', 0, 64)
	default:
		return strconv.FormatFloat(v, 'g', -1, 64)
	}
}
