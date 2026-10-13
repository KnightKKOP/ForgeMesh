module github.com/Mekoforge/ForgeMesh/gateway

// go 行也决定 GODEBUG 的默认值，落后于构建所用的 Go 次版本时二进制沿用旧版本的行为：
// 与 .github/workflows/ci.yml 的 GO_VERSION、deploy/docker/Dockerfile.gateway 一起升级。
go 1.27.0
