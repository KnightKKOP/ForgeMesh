// Command gateway 是 ForgeMesh 的 Go 高性能 API 网关入口。
//
// 启动流程：加载配置 → 构造路由与中间件链 → 启动 http.Server → 监听信号优雅停机。
//
// 优雅停机策略（对齐开发计划 §5.8）：
//  1. 收到 SIGINT/SIGTERM 后停止接收新连接；
//  2. 在排空窗口内等待在途请求（含 SSE 流）自然结束；
//  3. 超时则强制关闭。
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/Mekoforge/ForgeMesh/gateway/internal/config"
	"github.com/Mekoforge/ForgeMesh/gateway/internal/router"
)

// drainTimeout 是优雅停机的排空窗口。
const drainTimeout = 30 * time.Second

func main() {
	logger := log.New(os.Stdout, "gateway ", log.LstdFlags|log.LUTC)

	cfg, err := config.Load()
	if err != nil {
		logger.Fatalf("load config: %v", err)
	}

	routerHandler, err := router.New(cfg, logger)
	if err != nil {
		logger.Fatalf("init router: %v", err)
	}

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           routerHandler,
		ReadHeaderTimeout: 10 * time.Second,
		// 不设置 WriteTimeout：SSE/流式响应需要长连接。
		IdleTimeout: 120 * time.Second,
	}

	// 信号驱动：SIGINT/SIGTERM 触发 context 取消。
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		logger.Printf("listening on %s upstream=%s", cfg.ListenAddr, cfg.UpstreamURL)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		logger.Fatalf("server error: %v", err)
	case <-ctx.Done():
		logger.Print("shutdown signal received, draining in-flight requests...")
	}

	// 先停收新连接，再在排空窗口内等待在途请求结束。
	shutdownCtx, cancel := context.WithTimeout(context.Background(), drainTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		logger.Printf("graceful shutdown error: %v; forcing close", err)
		_ = srv.Close()
	}
	logger.Print("gateway stopped")
}
