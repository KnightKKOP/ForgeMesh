// Package metricstest 严格解析 Prometheus 文本格式（0.0.4），供测试校验 /metrics 的输出并读取
// 样本取值。
package metricstest

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

var (
	helpLine   = regexp.MustCompile(`^# HELP ([a-zA-Z_:][a-zA-Z0-9_:]*) ((?:[^\\]|\\[\\n])*)$`)
	typeLine   = regexp.MustCompile(`^# TYPE ([a-zA-Z_:][a-zA-Z0-9_:]*) (counter|gauge|histogram|summary|untyped)$`)
	sampleLine = regexp.MustCompile(`^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (\S+)$`)
	labelPair  = regexp.MustCompile(`^([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"`)
	floatValue = regexp.MustCompile(`^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$`)
)

type pair struct{ name, raw string }

// bucket 是直方图一个序列中的一个桶。
type bucket struct {
	le         float64
	cumulative float64
}

// Parse 严格解析 text，返回以「名称{标签}」（与输出相同的写法）为键的样本取值。
//
// 校验：以换行结束、没有空行与多余的注释；每个指标只声明一次 TYPE，样本紧跟在所属指标的
// TYPE 之后（直方图为 _bucket / _sum / _count）；标签名合法、不重复，取值只用 \\、\"、\n
// 转义；取值为浮点数或 +Inf / -Inf / NaN；样本不重复；直方图每个序列的 le 严格递增、累计值
// 不减，以 le="+Inf" 结束且等于 _count。
func Parse(text string) (map[string]float64, error) {
	if !strings.HasSuffix(text, "\n") {
		return nil, fmt.Errorf("输出必须以换行结束")
	}
	samples := make(map[string]float64)
	declared := make(map[string]bool)
	helps := make(map[string]bool)
	histograms := make(map[string][]bucket)
	var family, kind string
	for i, line := range strings.Split(strings.TrimSuffix(text, "\n"), "\n") {
		where := fmt.Sprintf("第 %d 行 %q", i+1, line)
		switch {
		case line == "":
			return nil, fmt.Errorf("%s: 空行", where)
		case strings.HasPrefix(line, "# HELP "):
			m := helpLine.FindStringSubmatch(line)
			if m == nil || helps[m[1]] {
				return nil, fmt.Errorf("%s: HELP 不合法或重复", where)
			}
			helps[m[1]] = true
		case strings.HasPrefix(line, "# TYPE "):
			m := typeLine.FindStringSubmatch(line)
			if m == nil || declared[m[1]] {
				return nil, fmt.Errorf("%s: TYPE 不合法或重复", where)
			}
			declared[m[1]] = true
			family, kind = m[1], m[2]
		case strings.HasPrefix(line, "#"):
			return nil, fmt.Errorf("%s: 多余的注释", where)
		default:
			m := sampleLine.FindStringSubmatch(line)
			if m == nil {
				return nil, fmt.Errorf("%s: 样本行不合法", where)
			}
			name, rawLabels, rawValue := m[1], m[2], m[3]
			if !belongs(name, family, kind) {
				return nil, fmt.Errorf("%s: 样本不属于前面声明的指标 %q（%s）", where, family, kind)
			}
			labels, err := parseLabels(rawLabels)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", where, err)
			}
			value, err := parseValue(rawValue)
			if err != nil {
				return nil, fmt.Errorf("%s: %w", where, err)
			}
			key := line[:len(line)-len(rawValue)-1]
			if _, dup := samples[key]; dup {
				return nil, fmt.Errorf("%s: 样本重复", where)
			}
			samples[key] = value
			if kind == "histogram" && name == family+"_bucket" {
				series, le, err := splitLE(family, labels)
				if err != nil {
					return nil, fmt.Errorf("%s: %w", where, err)
				}
				histograms[series] = append(histograms[series], bucket{le: le, cumulative: value})
			}
		}
	}
	for series, buckets := range histograms {
		if err := checkHistogram(series, buckets, samples); err != nil {
			return nil, err
		}
	}
	return samples, nil
}

// MustParse 同 Parse，格式不合法时令测试失败。
func MustParse(t testing.TB, text string) map[string]float64 {
	t.Helper()
	samples, err := Parse(text)
	if err != nil {
		t.Fatalf("指标输出不是合法的文本格式: %v\n%s", err, text)
	}
	return samples
}

func belongs(name, family, kind string) bool {
	switch kind {
	case "histogram":
		return name == family+"_bucket" || name == family+"_sum" || name == family+"_count"
	case "summary":
		return name == family || name == family+"_sum" || name == family+"_count"
	case "":
		return false
	default:
		return name == family
	}
}

func parseLabels(text string) ([]pair, error) {
	var pairs []pair
	seen := make(map[string]bool)
	for rest := text; rest != ""; {
		m := labelPair.FindStringSubmatch(rest)
		if m == nil || seen[m[1]] {
			return nil, fmt.Errorf("标签不合法或重复: %q", rest)
		}
		seen[m[1]] = true
		pairs = append(pairs, pair{name: m[1], raw: m[2]})
		rest = rest[len(m[0]):]
		if rest != "" {
			if rest[0] != ',' || len(rest) == 1 {
				return nil, fmt.Errorf("标签之间须以逗号分隔: %q", text)
			}
			rest = rest[1:]
		}
	}
	return pairs, nil
}

func parseValue(raw string) (float64, error) {
	switch raw {
	case "+Inf", "-Inf", "NaN":
	default:
		if !floatValue.MatchString(raw) {
			return 0, fmt.Errorf("取值不合法: %q", raw)
		}
	}
	return strconv.ParseFloat(raw, 64)
}

// splitLE 取出 le，返回去掉 le 之后的 _count 样本键与 le 的取值。
func splitLE(family string, labels []pair) (string, float64, error) {
	var rest []string
	le, found := 0.0, false
	for _, p := range labels {
		if p.name != "le" {
			rest = append(rest, p.name+`="`+p.raw+`"`)
			continue
		}
		v, err := parseValue(p.raw)
		if err != nil {
			return "", 0, fmt.Errorf("le %w", err)
		}
		le, found = v, true
	}
	if !found {
		return "", 0, fmt.Errorf("_bucket 样本缺少 le")
	}
	key := family + "_count"
	if len(rest) > 0 {
		key += "{" + strings.Join(rest, ",") + "}"
	}
	return key, le, nil
}

func checkHistogram(countKey string, buckets []bucket, samples map[string]float64) error {
	for i := 1; i < len(buckets); i++ {
		if buckets[i].le <= buckets[i-1].le || buckets[i].cumulative < buckets[i-1].cumulative {
			return fmt.Errorf("%s 的桶 le 须严格递增、累计值不减", countKey)
		}
	}
	last := buckets[len(buckets)-1]
	count, ok := samples[countKey]
	if !math.IsInf(last.le, 1) || !ok || count != last.cumulative {
		return fmt.Errorf("%s 须以 le=\"+Inf\" 结束且等于 _count", countKey)
	}
	return nil
}
