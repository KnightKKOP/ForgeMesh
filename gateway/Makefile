# ForgeMesh Go 网关 Makefile
#
# 用法：
#   make build   编译网关二进制到 bin/gateway
#   make test    运行全部 Go 单元测试（含 -race）
#   make run     本地启动网关（默认 apikey 认证，需要 GATEWAY_API_KEYS_FILE）
#   make run-dev 以 dev 认证模式在 127.0.0.1:8081 启动网关（信任租户头、只连接本机推理实例，仅限本地开发）
#   make fmt     格式化代码
#   make vet     静态检查
#   make clean   清理构建产物

GO      ?= go
BINARY  := bin/gateway
PKGS    := ./...
RACE_FLAGS := -race -count=1

.PHONY: all build test run run-dev fmt vet tidy clean

all: fmt vet build test

## build: 编译网关可执行文件
build:
	$(GO) build -o $(BINARY) ./cmd/gateway

## test: 运行单元测试（竞态检测）
test:
	$(GO) test $(RACE_FLAGS) $(PKGS)

## run: 本地运行网关（环境变量见 README）
run:
	$(GO) run ./cmd/gateway

## run-dev: 以 dev 认证模式在回环地址运行网关（信任租户头、只连接本机推理实例，仅限本地开发）
run-dev:
	GATEWAY_AUTH_MODE=dev GATEWAY_LISTEN_ADDR=127.0.0.1:8081 \
		GATEWAY_INSTANCE_ALLOWED_CIDRS=127.0.0.0/8,::1/128 $(GO) run ./cmd/gateway

## fmt: 格式化所有 Go 源文件
fmt:
	$(GO) fmt $(PKGS)

## vet: 运行 go vet 静态检查
vet:
	$(GO) vet $(PKGS)

## tidy: 整理模块依赖（无第三方依赖，通常无变更）
tidy:
	$(GO) mod tidy

## clean: 清理构建产物
clean:
	rm -rf bin