package main

import (
	"runtime/debug"
	"slices"
	"strings"
	"testing"
)

// 这些设置的旧默认值关掉网关依赖的行为：不限制 URL 查询参数个数（反代还会重新编码转发的查询串）、
// GOMAXPROCS 不考虑容器的 CPU 限制。
var requiredGODEBUGDefaults = []string{"urlmaxqueryparams", "containermaxprocs", "updatemaxprocs"}

// 构建信息中的 DefaultGODEBUG 列出因 go.mod 的 go 行较旧而沿用旧默认值的设置。
func TestBuildUsesCurrentGODEBUGDefaults(t *testing.T) {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		t.Fatal("测试二进制没有构建信息")
	}
	for _, s := range info.Settings {
		if s.Key != "DefaultGODEBUG" {
			continue
		}
		for _, kv := range strings.Split(s.Value, ",") {
			name, _, _ := strings.Cut(kv, "=")
			if slices.Contains(requiredGODEBUGDefaults, name) {
				t.Errorf("构建沿用了旧的默认值 %s：gateway/go.mod 的 go 行落后于构建所用的 Go 版本", kv)
			}
		}
	}
}
